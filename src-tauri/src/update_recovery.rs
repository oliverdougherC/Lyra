//! Retain a separately verified prior app before the supported installer can move it.
use std::path::{Path, PathBuf};
use std::time::Duration;
use tauri::{AppHandle, Manager};
use tauri_plugin_opener::OpenerExt;

fn recovery_root(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|path| path.join("update-recovery"))
        .map_err(|_| "Cannot locate app recovery storage.".into())
}
fn verify_app(path: &Path) -> Result<(), String> {
    let output = crate::bounded_process::run(
        std::process::Command::new("/usr/bin/codesign")
            .args([
                "--verify",
                "--deep",
                "--strict",
                "--test-requirement",
                "identifier \"com.lyra.desktop\"",
            ])
            .arg(path),
        Duration::from_secs(30),
    )
    .map_err(|_| "Retained application signature verification failed or timed out.")?;
    let status = output.status;
    if !status.success() {
        return Err(
            "The retained application signature could not be verified. Update stopped.".into(),
        );
    }
    Ok(())
}
fn tree_hashes(root: &Path) -> Result<std::collections::BTreeMap<PathBuf, Vec<u8>>, String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    fn walk(
        root: &Path,
        path: &Path,
        hashes: &mut std::collections::BTreeMap<PathBuf, Vec<u8>>,
    ) -> Result<(), String> {
        let metadata = path
            .symlink_metadata()
            .map_err(|_| "Cannot inspect app recovery copy.")?;
        let relative = path
            .strip_prefix(root)
            .map_err(|_| "Invalid recovery tree.")?
            .to_owned();
        let value = if metadata.file_type().is_symlink() {
            use std::os::unix::ffi::OsStrExt;
            let mut target = b"link:".to_vec();
            target.extend_from_slice(
                std::fs::read_link(path)
                    .map_err(|_| "Cannot inspect recovery link.")?
                    .as_os_str()
                    .as_bytes(),
            );
            target
        } else if metadata.is_dir() {
            for entry in
                std::fs::read_dir(path).map_err(|_| "Cannot inspect recovery directory.")?
            {
                walk(
                    root,
                    &entry.map_err(|_| "Cannot inspect recovery entry.")?.path(),
                    hashes,
                )?;
            }
            b"directory".to_vec()
        } else if metadata.is_file() {
            let mut file = std::fs::File::open(path).map_err(|_| "Cannot verify recovery file.")?;
            let mut digest = Sha256::new();
            let mut bytes = [0; 65536];
            loop {
                let count = file
                    .read(&mut bytes)
                    .map_err(|_| "Cannot verify recovery file.")?;
                if count == 0 {
                    break;
                }
                digest.update(&bytes[..count]);
            }
            digest.finalize().to_vec()
        } else {
            return Err("The app recovery copy contains a special file.".into());
        };
        hashes.insert(relative, value);
        Ok(())
    }
    let mut hashes = std::collections::BTreeMap::new();
    walk(root, root, &mut hashes)?;
    Ok(hashes)
}
fn verify_copy(source: &Path, backup: &Path) -> Result<(), String> {
    if tree_hashes(source)? != tree_hashes(backup)? {
        return Err(
            "The retained app does not match the current app byte-for-byte. Update stopped.".into(),
        );
    }
    Ok(())
}

fn sync_tree(path: &Path) -> Result<(), String> {
    let metadata = path
        .symlink_metadata()
        .map_err(|_| "Cannot inspect retained app.")?;
    if metadata.file_type().is_symlink() {
        return Ok(());
    }
    if metadata.is_dir() {
        for entry in std::fs::read_dir(path).map_err(|_| "Cannot read retained app.")? {
            sync_tree(&entry.map_err(|_| "Cannot read retained app entry.")?.path())?;
        }
    }
    std::fs::File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|_| "Cannot sync retained app before replacement.".into())
}
pub(crate) fn retain(app: &AppHandle) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    let exe = std::env::current_exe().map_err(|_| "Cannot locate the current app.")?;
    let current = exe
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .ok_or("Cannot locate the current app bundle.")?;
    if current.extension().and_then(|value| value.to_str()) != Some("app") {
        return Err("Install the packaged app before updating.".into());
    }
    verify_app(current)?;
    let root = recovery_root(app)?;
    std::fs::create_dir_all(&root).map_err(|_| "Cannot create recovery storage.")?;
    if root
        .symlink_metadata()
        .map_err(|_| "Cannot inspect recovery storage.")?
        .file_type()
        .is_symlink()
    {
        return Err("Recovery storage cannot be a symlink.".into());
    }
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700))
        .map_err(|_| "Cannot secure recovery storage.")?;
    let directory =
        root.join(crate::random_secret_hex().map_err(|_| "Cannot name the retained app.")?);
    std::fs::create_dir(&directory).map_err(|_| "Cannot create a private app backup.")?;
    std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))
        .map_err(|_| "Cannot secure the app backup.")?;
    let backup = directory.join("Lyra.app");
    // ditto preserves bundle symlinks, extended attributes and resource forks. This
    // is a backup operation only; all application replacement stays in Tauri.
    let output = crate::bounded_process::run(
        std::process::Command::new("/usr/bin/ditto")
            .arg(current)
            .arg(&backup),
        Duration::from_secs(120),
    )
    .map_err(|_| "Retaining the current app failed or timed out. Update stopped.")?;
    let status = output.status;
    if !status.success() {
        return Err("Could not retain the current app. Update stopped.".into());
    }
    verify_app(&backup)?;
    verify_copy(current, &backup)?;
    sync_tree(&backup)?;
    // Publish only a completely copied and signature-verified backup. Retain every
    // previous copy; a failed future update cannot remove the last recovery app.
    let receipt = root.join("latest.txt");
    let stage = directory.join("recovery-receipt.tmp");
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut receipt_file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&stage)
        .map_err(|_| "Cannot create app recovery record.")?;
    receipt_file
        .write_all(directory.file_name().unwrap().to_string_lossy().as_bytes())
        .and_then(|_| receipt_file.sync_all())
        .map_err(|_| "Cannot sync app recovery record.")?;
    std::fs::rename(stage, receipt).map_err(|_| "Cannot publish app recovery record.")?;
    for path in [&directory, &root] {
        std::fs::File::open(path)
            .and_then(|file| file.sync_all())
            .map_err(|_| "Cannot sync recovery directory.")?;
    }
    Ok(())
}
pub(crate) fn available(app: &AppHandle) -> bool {
    selected(app).is_ok()
}
fn selected(app: &AppHandle) -> Result<PathBuf, String> {
    let root = recovery_root(app)?;
    let selected = std::fs::read_to_string(root.join("latest.txt"))
        .map_err(|_| "No previous application has been retained yet.")?;
    if selected.len() != 64 || !selected.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("The app recovery record is invalid.".into());
    }
    let path = root.join(selected).join("Lyra.app");
    if !path.is_dir() {
        return Err("The retained app is not available.".into());
    }
    Ok(path)
}
#[tauri::command]
pub(crate) async fn desktop_update_recovery(app: AppHandle) -> Result<(), String> {
    let path = selected(&app)?;
    let verified = path.clone();
    tauri::async_runtime::spawn_blocking(move || verify_app(&verified))
        .await
        .map_err(|_| "Recovery verification stopped unexpectedly.")??;
    app.opener()
        .reveal_item_in_dir(path)
        .map_err(|_| "Could not reveal the retained app in Finder.".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn retained_bundle_verification_detects_missing_or_corrupt_sidecar() {
        let directory = std::env::temp_dir().join(format!(
            "lyra-recovery-test-{}",
            crate::random_secret_hex().unwrap()
        ));
        let source = directory.join("source/Lyra.app");
        let backup = directory.join("backup/Lyra.app");
        for app in [&source, &backup] {
            std::fs::create_dir_all(app.join("Contents/Resources")).unwrap();
            std::fs::write(
                app.join("Contents/Resources/backend"),
                b"frozen backend bytes",
            )
            .unwrap();
        }
        assert!(verify_copy(&source, &backup).is_ok());
        std::fs::write(
            backup.join("Contents/Resources/backend"),
            b"corrupt backend",
        )
        .unwrap();
        assert!(verify_copy(&source, &backup).is_err());
        std::fs::remove_file(backup.join("Contents/Resources/backend")).unwrap();
        assert!(verify_copy(&source, &backup).is_err());
        assert_eq!(
            std::fs::read(source.join("Contents/Resources/backend")).unwrap(),
            b"frozen backend bytes"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }
}
