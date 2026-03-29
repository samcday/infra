use anyhow::{Context, Result, bail};
use std::path::PathBuf;
use std::process::Command;

pub const HUB_SERVER_URL: &str = "https://10.0.1.254:6443";
pub const CERT_VALIDITY_DAYS: u32 = 1;
pub const CACHE_EXPIRY_MARGIN_SECS: i64 = 300;
pub const CERT_SUBJECT_CN: &str = "kubernetes-admin";
pub const CERT_SUBJECT_ORG: &str = "system:masters";

pub fn child_namespace(cluster: &str) -> Option<&'static str> {
    match cluster {
        "cloud" => Some("cloud-cluster"),
        _ => None,
    }
}

pub fn all_child_clusters() -> &'static [&'static str] {
    &["cloud"]
}

pub fn cache_root() -> PathBuf {
    let base = std::env::var("XDG_CACHE_HOME")
        .unwrap_or_else(|_| {
            let home = std::env::var("HOME").expect("HOME not set");
            format!("{home}/.cache")
        });
    PathBuf::from(base).join("infra-kube-auth")
}

pub fn repo_root() -> Result<PathBuf> {
    let output = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .context("failed to run git")?;
    if !output.status.success() {
        bail!("git rev-parse failed: {}", String::from_utf8_lossy(&output.stderr));
    }
    let path = String::from_utf8(output.stdout)
        .context("git output not utf8")?
        .trim()
        .to_string();
    Ok(PathBuf::from(path))
}
