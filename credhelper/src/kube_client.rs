use anyhow::{Context, Result, bail};
use k8s_openapi::api::core::v1::Secret;
use kube::api::Api;
use kube::config::{AuthInfo, Cluster, KubeConfigOptions, Kubeconfig, NamedAuthInfo, NamedCluster, NamedContext};
use kube::{Client, Config};
use std::path::Path;

pub async fn fetch_ca_secret(
    server_url: &str,
    server_tls_name: Option<&str>,
    server_ca_path: &Path,
    client_cert_path: &Path,
    client_key_path: &Path,
    namespace: &str,
) -> Result<(Vec<u8>, Vec<u8>)> {
    let kubeconfig = Kubeconfig {
        clusters: vec![NamedCluster {
            name: "hub".to_string(),
            cluster: Some(Cluster {
                server: Some(server_url.to_string()),
                tls_server_name: server_tls_name.map(str::to_string),
                certificate_authority: Some(server_ca_path.to_string_lossy().to_string()),
                ..Default::default()
            }),
        }],
        auth_infos: vec![NamedAuthInfo {
            name: "hub-admin".to_string(),
            auth_info: Some(AuthInfo {
                client_certificate: Some(client_cert_path.to_string_lossy().to_string()),
                client_key: Some(client_key_path.to_string_lossy().to_string()),
                ..Default::default()
            }),
        }],
        contexts: vec![NamedContext {
            name: "hub".to_string(),
            context: Some(kube::config::Context {
                cluster: "hub".to_string(),
                user: Some("hub-admin".to_string()),
                namespace: Some(namespace.to_string()),
                ..Default::default()
            }),
        }],
        current_context: Some("hub".to_string()),
        ..Default::default()
    };

    let config = Config::from_custom_kubeconfig(
        kubeconfig,
        &KubeConfigOptions::default(),
    )
    .await
    .context("failed to build kube config from cert files")?;

    let client = Client::try_from(config)
        .context("failed to create kube client")?;

    let secrets: Api<Secret> = Api::namespaced(client, namespace);
    let secret = secrets
        .get("ca")
        .await
        .with_context(|| format!("failed to get secret 'ca' in namespace '{namespace}'"))?;

    let data = secret.data.context("secret 'ca' has no data")?;

    let tls_key = data
        .get("tls.key")
        .context("secret 'ca' missing tls.key")?
        .0
        .clone();
    let tls_crt = data
        .get("tls.crt")
        .context("secret 'ca' missing tls.crt")?
        .0
        .clone();

    if tls_key.is_empty() || tls_crt.is_empty() {
        bail!("secret 'ca' has empty tls.key or tls.crt");
    }

    Ok((tls_key, tls_crt))
}
