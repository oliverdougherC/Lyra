//! Validate the *signed archive*, not just the unsigned transport manifest.
use std::collections::HashSet;
use std::io::{Cursor, Read};
use std::path::{Component, Path};

const MAX_UNPACKED: u64 = 2 * 1024 * 1024 * 1024;

fn safe_path(path: &Path) -> Result<String, String> {
    let mut names = Vec::new();
    for part in path.components() {
        match part {
            Component::Normal(name) => names.push(name.to_str().ok_or("Non-UTF8 archive path.")?),
            Component::CurDir => (),
            _ => return Err("An update archive path escapes the application.".into()),
        }
    }
    if names.first() != Some(&"Lyra.app") {
        return Err("The archive is not a Lyra application.".into());
    }
    Ok(names.join("/"))
}
fn safe_link(path: &Path, target: &Path, hardlink: bool) -> Result<(), String> {
    if target.is_absolute() {
        return Err("The update contains an absolute symlink.".into());
    }
    let mut depth = if hardlink {
        0
    } else {
        path.components().count().saturating_sub(1)
    };
    for part in target.components() {
        match part {
            Component::Normal(_) => depth += 1,
            Component::CurDir => (),
            Component::ParentDir if depth > 1 => depth -= 1,
            _ => return Err("The update contains a link outside its application.".into()),
        }
    }
    if hardlink {
        safe_path(target)?;
    }
    Ok(())
}
pub(crate) fn validate(
    bytes: &[u8],
    version: &str,
    feed: &serde_json::Value,
) -> Result<u64, String> {
    let decoder = flate2::read::GzDecoder::new(Cursor::new(bytes));
    let mut archive = tar::Archive::new(decoder);
    let mut paths = HashSet::new();
    let mut total = 0u64;
    let mut contract = None;
    let mut info = None;
    let mut executable = None;
    for entry in archive
        .entries()
        .map_err(|_| "The update archive is corrupt.")?
    {
        let mut entry = entry.map_err(|_| "The update archive is corrupt.")?;
        let path = entry
            .path()
            .map_err(|_| "Invalid update archive path.")?
            .into_owned();
        let name = safe_path(&path)?;
        if !paths.insert(name.clone()) || paths.len() > 100_000 {
            return Err("The update archive contains duplicate or excessive entries.".into());
        }
        let kind = entry.header().entry_type();
        if kind.is_symlink() || kind.is_hard_link() {
            let link = entry
                .link_name()
                .map_err(|_| "Invalid archive link.")?
                .ok_or("Missing archive link.")?;
            safe_link(Path::new(&name), &link, kind.is_hard_link())?;
        } else if !kind.is_file() && !kind.is_dir() {
            return Err("The update contains an unsupported special file.".into());
        }
        total = total
            .checked_add(entry.size())
            .ok_or("Archive size overflow.")?;
        if total > MAX_UNPACKED {
            return Err("The expanded update exceeds the supported size.".into());
        }
        if name == "Lyra.app/Contents/Resources/lyra-release.json"
            || name == "Lyra.app/Contents/Info.plist"
        {
            if !kind.is_file() || entry.size() > 64 * 1024 {
                return Err("Invalid application identity file.".into());
            }
            let mut contents = Vec::new();
            entry
                .read_to_end(&mut contents)
                .map_err(|_| "Cannot read application identity.")?;
            if name.ends_with("lyra-release.json") {
                contract = Some(contents);
            } else {
                info = Some(contents);
            }
        }
        if name == "Lyra.app/Contents/MacOS/lyra-desktop" {
            if !kind.is_file() {
                return Err("Invalid application executable.".into());
            }
            let mut header = [0; 8];
            entry
                .read_exact(&mut header)
                .map_err(|_| "Truncated application executable.")?;
            executable = Some(header);
        }
    }
    let contract: serde_json::Value =
        serde_json::from_slice(&contract.ok_or("The signed update has no release identity.")?)
            .map_err(|_| "The signed release identity is invalid.")?;
    if contract["version"] != version
        || contract["bundleIdentifier"] != "com.lyra.desktop"
        || contract["architecture"] != "aarch64"
    {
        return Err("The signed application identity does not match this update. Possible old-release replay.".into());
    }
    for key in ["schemaMin", "schemaMax"] {
        if contract[key].as_u64().is_none() || contract[key] != feed["lyra"][key] {
            return Err("The signed schema contract disagrees with the feed.".into());
        }
    }
    let info = plist::Value::from_reader(Cursor::new(
        info.ok_or("The signed update has no Info.plist.")?,
    ))
    .map_err(|_| "Invalid signed application Info.plist.")?;
    let info = info
        .as_dictionary()
        .ok_or("Invalid signed application Info.plist.")?;
    let field = |key: &str| info.get(key).and_then(plist::Value::as_string);
    if field("CFBundleIdentifier") != Some("com.lyra.desktop")
        || field("CFBundleExecutable") != Some("lyra-desktop")
        || (field("CFBundleShortVersionString") != Some(version)
            && field("CFBundleShortVersionString") != version.split('-').next())
        || field("CFBundleVersion") != contract["build"].as_str()
    {
        return Err(
            "The actual signed bundle version or identity disagrees with the update.".into(),
        );
    }
    // Apple Silicon-only beta: little-endian 64-bit Mach-O, CPU_TYPE_ARM64.
    if executable != Some([0xcf, 0xfa, 0xed, 0xfe, 0x0c, 0, 0, 1]) {
        return Err("The signed application executable is not Apple Silicon.".into());
    }
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn archive(version: &str, cpu: [u8; 8]) -> Vec<u8> {
        let mut archive = tar::Builder::new(Vec::new());
        let info = format!("<?xml version=\"1.0\"?><plist version=\"1.0\"><dict><key>CFBundleIdentifier</key><string>com.lyra.desktop</string><key>CFBundleExecutable</key><string>lyra-desktop</string><key>CFBundleShortVersionString</key><string>{version}</string><key>CFBundleVersion</key><string>42</string></dict></plist>");
        let contract = serde_json::json!({"version":version,"build":"42","bundleIdentifier":"com.lyra.desktop","architecture":"aarch64","schemaMin":0,"schemaMax":45}).to_string();
        for (path, bytes) in [
            ("Lyra.app/Contents/Info.plist", info.as_bytes()),
            (
                "Lyra.app/Contents/Resources/lyra-release.json",
                contract.as_bytes(),
            ),
            ("Lyra.app/Contents/MacOS/lyra-desktop", cpu.as_slice()),
        ] {
            let mut header = tar::Header::new_gnu();
            header.set_size(bytes.len() as u64);
            header.set_mode(0o600);
            header.set_cksum();
            archive.append_data(&mut header, path, bytes).unwrap();
        }
        let data = archive.into_inner().unwrap();
        let mut gzip = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
        std::io::Write::write_all(&mut gzip, &data).unwrap();
        gzip.finish().unwrap()
    }
    #[test]
    fn signed_old_archive_cannot_replay_under_new_feed_version() {
        let feed = serde_json::json!({"lyra":{"schemaMin":0,"schemaMax":45}});
        let bytes = archive("0.2.0-beta.1", [0xcf, 0xfa, 0xed, 0xfe, 0x0c, 0, 0, 1]);
        assert!(validate(&bytes, "0.2.0-beta.1", &feed).is_ok());
        assert!(validate(&bytes, "0.2.0-beta.2", &feed)
            .unwrap_err()
            .contains("replay"));
        assert!(validate(
            &bytes,
            "0.2.0-beta.1",
            &serde_json::json!({"lyra":{"schemaMin":0,"schemaMax":46}})
        )
        .is_err());
    }
    #[test]
    fn actual_intel_binary_and_corrupt_archive_are_refused() {
        let feed = serde_json::json!({"lyra":{"schemaMin":0,"schemaMax":45}});
        assert!(validate(
            &archive("0.2.0-beta.1", [0xcf, 0xfa, 0xed, 0xfe, 7, 0, 0, 1]),
            "0.2.0-beta.1",
            &feed
        )
        .is_err());
        assert!(validate(b"corrupt", "0.2.0-beta.1", &feed).is_err());
    }
    #[test]
    fn links_cannot_escape_app() {
        assert!(safe_link(
            Path::new("Lyra.app/Contents/link"),
            Path::new("../../../outside"),
            false
        )
        .is_err());
        assert!(safe_link(
            Path::new("Lyra.app/Contents/link"),
            Path::new("Resources"),
            false
        )
        .is_ok());
    }
}
