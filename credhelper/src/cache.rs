use anyhow::{Context, Result};
use std::fs;
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use crate::config;
use crate::pki;

pub struct CachePaths {
    pub dir: PathBuf,
    pub client_cert: PathBuf,
    pub client_key: PathBuf,
    pub server_ca: PathBuf,
}

impl CachePaths {
    pub fn for_cluster(cluster: &str) -> Self {
        let dir = config::cache_root().join(cluster);
        Self {
            client_cert: dir.join("client.crt"),
            client_key: dir.join("client.key"),
            server_ca: dir.join("server-ca.crt"),
            dir,
        }
    }
}

pub fn ensure_dir(paths: &CachePaths) -> Result<()> {
    fs::create_dir_all(&paths.dir)
        .with_context(|| format!("failed to create cache dir: {}", paths.dir.display()))?;
    fs::set_permissions(&paths.dir, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

pub fn is_valid(paths: &CachePaths) -> bool {
    is_valid_inner(paths).unwrap_or(false)
}

fn is_valid_inner(paths: &CachePaths) -> Result<bool> {
    if !paths.client_cert.exists() || !paths.client_key.exists() {
        return Ok(false);
    }
    let meta_cert = fs::metadata(&paths.client_cert)?;
    let meta_key = fs::metadata(&paths.client_key)?;
    if meta_cert.len() == 0 || meta_key.len() == 0 {
        return Ok(false);
    }
    let cert_pem = fs::read_to_string(&paths.client_cert)?;
    Ok(!pki::cert_expires_within(&cert_pem, config::CACHE_EXPIRY_MARGIN_SECS)?)
}

pub fn write_cert(path: &Path, content: &str) -> Result<()> {
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o644)
        .open(path)
        .with_context(|| format!("failed to write cert: {}", path.display()))?;
    f.write_all(content.as_bytes())?;
    Ok(())
}

pub fn write_key(path: &Path, content: &str) -> Result<()> {
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("failed to write key: {}", path.display()))?;
    f.write_all(content.as_bytes())?;
    Ok(())
}

#[allow(dead_code)]
pub fn secure_delete(path: &Path) -> Result<()> {
    if !path.exists() {
        return Ok(());
    }
    let len = fs::metadata(path)?.len() as usize;
    if len > 0 {
        let zeros = vec![0u8; len];
        fs::write(path, &zeros)?;
    }
    fs::remove_file(path)?;
    Ok(())
}
