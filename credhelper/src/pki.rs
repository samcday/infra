use anyhow::{Context, Result};
use der::DecodePem;
use rcgen::{
    CertificateParams, DnType, KeyPair, SerialNumber, PKCS_ECDSA_P256_SHA256,
};
use std::time::{SystemTime, UNIX_EPOCH};
use time::OffsetDateTime;
use x509_cert::Certificate;
use zeroize::Zeroizing;

use crate::config;

pub fn cert_not_after(cert_pem: &str) -> Result<OffsetDateTime> {
    let cert = Certificate::from_pem(cert_pem)
        .context("failed to parse certificate PEM")?;
    let not_after = cert.tbs_certificate.validity.not_after;
    let dt = not_after.to_date_time();
    let unix = dt.unix_duration().as_secs() as i64;
    OffsetDateTime::from_unix_timestamp(unix).context("invalid timestamp")
}

pub fn cert_expires_within(cert_pem: &str, seconds: i64) -> Result<bool> {
    let not_after = cert_not_after(cert_pem)?;
    let margin = time::Duration::seconds(seconds);
    let now = OffsetDateTime::now_utc();
    Ok(not_after - now < margin)
}

pub fn format_timestamp(dt: OffsetDateTime) -> String {
    dt.format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_default()
}

/// Convert a private key PEM to PKCS#8 PEM format.
/// rcgen's ring backend only accepts PKCS#8, but CA keys may be SEC1 or RSA PKCS#1.
fn to_pkcs8(pem_str: &str) -> Result<String> {
    let parsed = pem::parse(pem_str).context("failed to parse PEM")?;
    match parsed.tag() {
        "PRIVATE KEY" => Ok(pem_str.to_string()),
        "EC PRIVATE KEY" => wrap_pkcs8(
            parsed.contents(),
            // AlgorithmIdentifier: ecPublicKey + prime256v1
            &[
                0x30, 0x13,
                0x06, 0x07, 0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x02, 0x01,
                0x06, 0x08, 0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x03, 0x01, 0x07,
            ],
        ),
        "RSA PRIVATE KEY" => wrap_pkcs8(
            parsed.contents(),
            // AlgorithmIdentifier: rsaEncryption + NULL
            &[
                0x30, 0x0D,
                0x06, 0x09, 0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x01, 0x01,
                0x05, 0x00,
            ],
        ),
        tag => anyhow::bail!("unexpected PEM tag: {tag}"),
    }
}

fn wrap_pkcs8(key_der: &[u8], alg_id: &[u8]) -> Result<String> {
    let mut inner = Vec::new();
    inner.extend_from_slice(&[0x02, 0x01, 0x00]); // INTEGER 0 (version)
    inner.extend_from_slice(alg_id);
    inner.push(0x04); // OCTET STRING
    der_encode_length(&mut inner, key_der.len());
    inner.extend_from_slice(key_der);

    let mut pkcs8_der = Vec::new();
    pkcs8_der.push(0x30); // SEQUENCE
    der_encode_length(&mut pkcs8_der, inner.len());
    pkcs8_der.extend_from_slice(&inner);

    Ok(pem::encode(&pem::Pem::new("PRIVATE KEY", pkcs8_der)))
}

fn der_encode_length(buf: &mut Vec<u8>, len: usize) {
    if len < 128 {
        buf.push(len as u8);
    } else if len < 256 {
        buf.push(0x81);
        buf.push(len as u8);
    } else {
        buf.push(0x82);
        buf.push((len >> 8) as u8);
        buf.push((len & 0xFF) as u8);
    }
}

pub fn issue_client_cert(
    ca_cert_pem: &str,
    ca_key_pem: &str,
) -> Result<(String, Zeroizing<String>)> {
    let ca_key_pkcs8 = to_pkcs8(ca_key_pem)
        .context("failed to convert CA key to PKCS#8")?;
    let ca_key = KeyPair::from_pem(&ca_key_pkcs8)
        .context("failed to parse CA key PEM")?;
    let ca_params = CertificateParams::from_ca_cert_pem(ca_cert_pem)
        .context("failed to parse CA cert for rcgen")?;
    let ca_cert = ca_params.self_signed(&ca_key)
        .context("failed to reconstruct CA cert")?;

    let serial = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();

    let now = OffsetDateTime::now_utc();
    let expiry = now + time::Duration::days(config::CERT_VALIDITY_DAYS as i64);

    let mut client_params = CertificateParams::default();
    client_params.distinguished_name.push(DnType::CommonName, config::CERT_SUBJECT_CN);
    client_params.distinguished_name.push(DnType::OrganizationName, config::CERT_SUBJECT_ORG);
    client_params.serial_number = Some(SerialNumber::from(serial));
    client_params.not_before = now;
    client_params.not_after = expiry;

    let client_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256)
        .context("failed to generate client key")?;
    let client_key_pem = Zeroizing::new(client_key.serialize_pem());

    let client_cert = client_params
        .signed_by(&client_key, &ca_cert, &ca_key)
        .context("failed to sign client certificate")?;
    let client_cert_pem = client_cert.pem();

    Ok((client_cert_pem, client_key_pem))
}
