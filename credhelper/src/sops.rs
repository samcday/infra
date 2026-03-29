use aes_gcm::aead::consts::U32;
use aes_gcm::aead::Aead;
use aes_gcm::{aes::Aes256, AesGcm, KeyInit, Nonce};
use anyhow::{bail, Context, Result};
use base64ct::{Base64, Encoding};
use serde::Deserialize;
use std::io::Read;
use std::path::Path;
use zeroize::Zeroizing;

#[derive(Deserialize)]
struct SopsFile {
    data: String,
    sops: SopsMeta,
}

#[derive(Deserialize)]
struct SopsMeta {
    age: Vec<AgeRecipient>,
}

#[derive(Deserialize)]
struct AgeRecipient {
    enc: String,
}

pub fn decrypt_file(path: &Path) -> Result<Zeroizing<String>> {
    let content = std::fs::read_to_string(path)
        .with_context(|| format!("failed to read SOPS file: {}", path.display()))?;
    let sops_file: SopsFile =
        serde_json::from_str(&content).context("failed to parse SOPS JSON")?;

    let data_key = decrypt_age_data_key(&sops_file.sops.age)
        .context("failed to decrypt SOPS data key with age")?;

    // SOPS uses "key:" as AAD (trailing colon in key paths)
    let plaintext = decrypt_sops_value(&sops_file.data, &data_key, "data:")
        .context("failed to decrypt SOPS data value")?;

    let text =
        String::from_utf8(plaintext.to_vec()).context("decrypted SOPS value is not valid UTF-8")?;
    Ok(Zeroizing::new(text))
}

fn age_identities() -> Result<Vec<Box<dyn age::Identity>>> {
    let key_file = std::env::var("SOPS_AGE_KEY_FILE").unwrap_or_else(|_| {
        let home = std::env::var("HOME").expect("HOME not set");
        format!("{home}/.config/sops/age/keys.txt")
    });
    let path = Path::new(&key_file);
    if !path.exists() {
        bail!("age key file not found: {key_file}");
    }
    let identities = age::IdentityFile::from_file(key_file.to_string())
        .context("failed to parse age identity file")?
        .into_identities()
        .context("failed to load age identities")?;
    Ok(identities)
}

fn decrypt_age_data_key(age_recipients: &[AgeRecipient]) -> Result<Zeroizing<Vec<u8>>> {
    if age_recipients.is_empty() {
        bail!("no age recipients in SOPS file");
    }

    let identities = age_identities()?;
    let identity_refs: Vec<&dyn age::Identity> = identities
        .iter()
        .map(|i| i.as_ref() as &dyn age::Identity)
        .collect();

    let mut errors = Vec::new();
    for (index, recipient) in age_recipients.iter().enumerate() {
        let attempt = (|| -> Result<Zeroizing<Vec<u8>>> {
            let armored = age::armor::ArmoredReader::new(recipient.enc.as_bytes());
            let decryptor =
                age::Decryptor::new(armored).context("failed to create age decryptor")?;
            let mut reader = decryptor
                .decrypt(identity_refs.iter().copied())
                .context("age decryption failed")?;
            let mut data_key = Vec::new();
            reader
                .read_to_end(&mut data_key)
                .context("failed to read decrypted data key")?;
            Ok(Zeroizing::new(data_key))
        })();

        match attempt {
            Ok(data_key) => return Ok(data_key),
            Err(err) => errors.push(format!("recipient #{index}: {err:#}")),
        }
    }

    bail!(
        "age decryption failed for all recipients: {}",
        errors.join("; ")
    );
}

/// Parse `ENC[AES256_GCM,data:<b64>,iv:<b64>,tag:<b64>,type:str]` and decrypt.
fn decrypt_sops_value(enc_str: &str, data_key: &[u8], aad: &str) -> Result<Zeroizing<Vec<u8>>> {
    let inner = enc_str
        .strip_prefix("ENC[AES256_GCM,")
        .and_then(|s| s.strip_suffix(']'))
        .context("invalid SOPS ENC[] format")?;

    let mut data_b64 = None;
    let mut iv_b64 = None;
    let mut tag_b64 = None;

    for part in inner.split(',') {
        let (key, value) = part
            .split_once(':')
            .context("invalid SOPS ENC[] key:value pair")?;
        match key {
            "data" => data_b64 = Some(value),
            "iv" => iv_b64 = Some(value),
            "tag" => tag_b64 = Some(value),
            "type" => {}
            _ => bail!("unknown SOPS ENC[] field: {key}"),
        }
    }

    let ciphertext = Base64::decode_vec(data_b64.context("missing data in ENC[]")?)
        .context("invalid base64 in ENC[] data")?;
    let iv = Base64::decode_vec(iv_b64.context("missing iv in ENC[]")?)
        .context("invalid base64 in ENC[] iv")?;
    let tag = Base64::decode_vec(tag_b64.context("missing tag in ENC[]")?)
        .context("invalid base64 in ENC[] tag")?;

    // AES-GCM expects ciphertext || tag
    let mut ct_with_tag = ciphertext;
    ct_with_tag.extend_from_slice(&tag);

    // SOPS uses 32-byte nonces (non-standard; Go's cipher.NewGCMWithNonceSize)
    type Aes256Gcm32 = AesGcm<Aes256, U32>;
    let cipher = Aes256Gcm32::new_from_slice(data_key).context("invalid AES-256 key length")?;
    let nonce = Nonce::<U32>::from_slice(&iv);

    let payload = aes_gcm::aead::Payload {
        msg: &ct_with_tag,
        aad: aad.as_bytes(),
    };
    let plaintext = cipher
        .decrypt(nonce, payload)
        .map_err(|e| anyhow::anyhow!("AES-GCM decryption failed: {e}"))?;

    Ok(Zeroizing::new(plaintext))
}
