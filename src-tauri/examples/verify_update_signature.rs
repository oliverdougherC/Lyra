//! Publisher verifier: the same maintained verifier and public key as the updater.
use base64::Engine;
#[path = "../src/update_archive.rs"]
mod update_archive;
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    if arguments.len() != 2 && !(arguments.len() == 3 && arguments[2] == "--check-archive") {
        return Err("usage: verify_update_signature ARCHIVE SIGNATURE [--check-archive]".into());
    }
    verify(
        &std::fs::read(&arguments[0])?,
        &std::fs::read_to_string(&arguments[1])?,
    )?;
    if arguments.len() == 3 {
        let schema: u64 = env!("LYRA_SCHEMA_VERSION").parse()?;
        let feed = serde_json::json!({"lyra": {"schemaMin": 0, "schemaMax": schema}});
        let size = update_archive::validate(
            &std::fs::read(&arguments[0])?,
            env!("CARGO_PKG_VERSION"),
            &feed,
        )?;
        println!(
            "Actual updater archive accepted by the installed parser ({size} unpacked bytes)."
        );
    }
    println!("Updater archive signature verified against the retained Lyra public key.");
    Ok(())
}

fn verify(bytes: &[u8], signature: &str) -> Result<(), Box<dyn std::error::Error>> {
    let decode = |input: &str| -> Result<String, Box<dyn std::error::Error>> {
        Ok(String::from_utf8(
            base64::engine::general_purpose::STANDARD.decode(input.trim())?,
        )?)
    };
    let key =
        minisign_verify::PublicKey::decode(&decode(include_str!("../updater-public-key.txt"))?)?;
    let signature = minisign_verify::Signature::decode(&decode(signature)?)?;
    key.verify(bytes, &signature, true)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    const MESSAGE: &[u8] = include_bytes!("../../scripts/release/fixtures/signature-check.txt");
    const SIGNATURE: &str = include_str!("../../scripts/release/fixtures/signature-check.txt.sig");
    #[test]
    fn persistent_key_signature_is_accepted() {
        assert!(verify(MESSAGE, SIGNATURE).is_ok());
    }
    #[test]
    fn corrupt_payload_is_rejected() {
        assert!(verify(b"corrupted payload", SIGNATURE).is_err());
    }
    #[test]
    fn unrelated_key_signature_is_rejected() {
        assert!(verify(
            MESSAGE,
            include_str!("../../scripts/release/fixtures/wrong-key.txt.sig")
        )
        .is_err());
    }
}
