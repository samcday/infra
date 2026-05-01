mod cache;
mod config;
mod kube_client;
mod pki;
mod sops;

use anyhow::{Context, Result, bail};
use serde::Serialize;
use std::fs;
use zeroize::Zeroizing;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ExecCredential {
    api_version: &'static str,
    kind: &'static str,
    status: ExecCredentialStatus,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ExecCredentialStatus {
    expiration_timestamp: String,
    client_certificate_data: String,
    client_key_data: String,
}

fn emit_exec_credential(paths: &cache::CachePaths) -> Result<()> {
    let cert_pem = fs::read_to_string(&paths.client_cert)
        .context("failed to read cached client cert")?;
    let key_pem = Zeroizing::new(
        fs::read_to_string(&paths.client_key)
            .context("failed to read cached client key")?,
    );
    let not_after = pki::cert_not_after(&cert_pem)?;
    let cred = ExecCredential {
        api_version: "client.authentication.k8s.io/v1",
        kind: "ExecCredential",
        status: ExecCredentialStatus {
            expiration_timestamp: pki::format_timestamp(not_after),
            client_certificate_data: cert_pem,
            client_key_data: key_pem.to_string(),
        },
    };
    serde_json::to_writer(std::io::stdout(), &cred)
        .context("failed to write ExecCredential JSON")?;
    Ok(())
}

fn ensure_hub_credentials() -> Result<cache::CachePaths> {
    let paths = cache::CachePaths::for_cluster("hub");

    if cache::is_valid(&paths) {
        return Ok(paths);
    }

    let root = config::repo_root()?;
    let ca_cert_path = root.join("hub/pki/k8s/ca.crt");
    let ca_key_enc_path = root.join("hub/pki/k8s/ca-key.pem.enc");

    if !ca_cert_path.exists() {
        bail!("missing hub CA cert: {}", ca_cert_path.display());
    }
    if !ca_key_enc_path.exists() {
        bail!("missing encrypted hub CA key: {}", ca_key_enc_path.display());
    }

    let ca_cert_pem = fs::read_to_string(&ca_cert_path)
        .context("failed to read hub CA cert")?;
    let ca_key_pem = sops::decrypt_file(&ca_key_enc_path)
        .context("failed to decrypt hub CA key")?;

    let (client_cert_pem, client_key_pem) =
        pki::issue_client_cert(&ca_cert_pem, &ca_key_pem)?;

    cache::ensure_dir(&paths)?;
    cache::write_cert(&paths.client_cert, &client_cert_pem)?;
    cache::write_key(&paths.client_key, &client_key_pem)?;

    Ok(paths)
}

async fn ensure_child_credentials(cluster: &str, init: bool) -> Result<cache::CachePaths> {
    let namespace = config::child_namespace(cluster)
        .with_context(|| format!("unsupported child cluster: {cluster}"))?;

    let paths = cache::CachePaths::for_cluster(cluster);
    let root = config::repo_root()?;
    let repo_server_ca = root.join(format!("hub/pki/k8s/{cluster}-server-ca.crt"));

    let cache_ok = cache::is_valid(&paths) && paths.server_ca.exists();

    if cache_ok && repo_server_ca.exists() {
        return Ok(paths);
    }

    if cache_ok && !repo_server_ca.exists() {
        eprintln!(
            "warning: repo server CA missing at {}, re-fetching from hub API...",
            repo_server_ca.display()
        );
    }

    if !init && !cache_ok && !repo_server_ca.exists() {
        bail!(
            "{cluster} server CA not found at {}. Run 'credhelper --init' first.",
            repo_server_ca.display()
        );
    }

    let (parent_server_url, parent_server_ca, parent_paths) = match cluster {
        "cloud" => {
            let hub_paths = ensure_hub_credentials()?;
            let hub_server_ca = root.join("hub/pki/k8s/server-ca.crt");
            (config::HUB_SERVER_URL, hub_server_ca, hub_paths)
        }
        "edge-au-east" => {
            let cloud_paths = Box::pin(ensure_child_credentials("cloud", init)).await?;
            let cloud_server_ca = root.join("hub/pki/k8s/cloud-server-ca.crt");
            (config::CLOUD_SERVER_URL, cloud_server_ca, cloud_paths)
        }
        _ => bail!("unsupported child cluster: {cluster}"),
    };

    let (child_ca_key, child_ca_cert) = kube_client::fetch_ca_secret(
        parent_server_url,
        &parent_server_ca,
        &parent_paths.client_cert,
        &parent_paths.client_key,
        namespace,
    )
    .await?;

    let child_ca_key_pem = Zeroizing::new(
        String::from_utf8(child_ca_key).context("child CA key is not valid UTF-8")?,
    );
    let child_ca_cert_pem =
        String::from_utf8(child_ca_cert).context("child CA cert is not valid UTF-8")?;

    let (client_cert_pem, client_key_pem) =
        pki::issue_client_cert(&child_ca_cert_pem, &child_ca_key_pem)?;

    cache::ensure_dir(&paths)?;
    cache::write_cert(&paths.client_cert, &client_cert_pem)?;
    cache::write_key(&paths.client_key, &client_key_pem)?;
    cache::write_cert(&paths.server_ca, &child_ca_cert_pem)?;
    cache::write_cert(&repo_server_ca, &child_ca_cert_pem)?;

    Ok(paths)
}

fn usage() {
    eprintln!(
        "Usage:
  credhelper <cluster-name>
  credhelper --init

Clusters:
  hub
  cloud
  edge-au-east"
    );
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let cluster = args.get(1).map(|s| s.as_str());

    match cluster {
        Some("--help" | "-h") => {
            usage();
        }
        Some("--init") => {
            for cluster in config::all_child_clusters() {
                ensure_child_credentials(cluster, true).await?;
            }
            eprintln!(
                "Initialized child cluster credentials in {}",
                config::cache_root().display()
            );
        }
        Some("hub") => {
            let paths = ensure_hub_credentials()?;
            emit_exec_credential(&paths)?;
        }
        Some(name) => {
            if config::child_namespace(name).is_some() {
                let paths = ensure_child_credentials(name, false).await?;
                emit_exec_credential(&paths)?;
            } else {
                bail!("unknown cluster: {name}");
            }
        }
        None => {
            usage();
            std::process::exit(1);
        }
    }

    Ok(())
}
