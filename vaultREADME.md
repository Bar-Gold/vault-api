# Vault Service Overview

This document provides an overview of our Vault service setup and instructions for teams to connect their spaces to the main Vault cluster, referred to as the **Central-Vault**.


## Overview

The Vault service is hosted on the **Central-Vault** and allows teams to securely manage their secrets and policies. Each team can configure their access by submitting a pull request to the **Vault repository** in Bitbucket.


---

## 🚀 Tashtiot Services Integration

For teams consuming or providing services within the Tashtiot ecosystem, follow these specific guidelines. Your configuration file should be placed in:  
**`KVs/Tashtiot/<Team-Name>.yaml`**

### 1. Service Consumer (Read-Only Access)
If you are **consuming** a service, you typically require **read-only** access.
**Only `read` capabilities are accepted.**

```yaml
access:
  - path: "+/cloudops/*"
    authType: oidc
    dn: "CN=<CN>,OU=<OU>,DC=<DC>"
    capabilities:
      - read
```

### 2. Service Provider (Full Access)
If you are **providing** a service, you may need **read/write** access for both OIDC groups and Kubernetes service accounts.

```yaml
access:
  - path: "services/+/tyk/*"
    authType: oidc
    dn: "CN=<CN>,OU=<OU>,DC=<DC>"
    capabilities:
      - read
      - write
  - path: "services/+/tyk/*"
    authType: k8s
    name: argocd              # Service account name
    namespace: perimeter         # Kubernetes namespace
    cluster: dev                   # Must match a k8sMethods[].name
    capabilities: 
      - read
      - write
```

---

### Setup Instructions (General Teams)

To configure your team’s access:

1. Navigate to the `KVs` folder under the **Vault repository** in Bitbucket.
2. Create a new folder for your team (e.g., `team-name`).
3. Inside your team folder, create a subfolder for your specific KV store (e.g., `my-kv-store`).
4. Inside the KV store folder, add the necessary YAML configuration files:
   - `oidc-access.yaml` (for OIDC/Group access)
   - `k8s-access.yaml` (for Kubernetes Service Account access, if applicable)

### Configuration Details

The new architecture splits access controls into two distinct files within a dedicated KV folder. This ensures clean separation of identity providers.

## ⚠️ Must Read

| **⚠️  Important** |
|:----------------:|
| <span style="color:#D32F2F; font-weight:bold;">**Naming Convention is as follows: <Team/Project>-<KV_Name>. The folder name must match this unique KV name.**</span> |
| <span style="color:#D32F2F; font-weight:bold;">**IN Drone the <Team DN should only contain the Group CN!>**</span> |
| <span style="color:#D32F2F; font-weight:bold;">**Capabilities: Use block-list format for capabilities (e.g., `- read` and `- write` on new lines).**</span> |


## Template Files

### 1. OIDC Access Template (`oidc-access.yaml`)
Use this file for Group/OIDC users.

```yaml
access:
  - path: "*"
    authType: oidc
    dn: "<Team DN>"
    capabilities:
      - read
      - write
```

### 2. K8s Access Template (`k8s-access.yaml`)
Use this file for Kubernetes Service Accounts.

```yaml
access:
  - path: "*"
    authType: k8s
    name: "<Service Account Name>"
    namespace: "<Namespace>"
    cluster: <OS Cluster e.g dev/rak/jon>
    capabilities:
      - read
      - write
```

## Real Example: `cloudops/my-passwords/`

### File: `oidc-access.yaml`
```yaml
access:
  - path: "*"
    authType: oidc
    dn: ""CN=<CN>,OU=<OU>,DC=<DC>""
    capabilities:
      - read
      - write
  - path: "*"
    authType: oidc
    dn: ""CN=<CN>,OU=<OU>,DC=<DC>""
    capabilities:
      - read
      - write
```

### File: `k8s-access.yaml`
```yaml
access:
  - path: "*"
    authType: k8s
    name: "cloudops-external-secrets"
    namespace: "cloudops"
    cluster: dev
    capabilities:
      - read
      - write
```

#### Fields Explanation

**Common Fields:**
- `path`: The secret path pattern. Use `"*"` for all secrets in this KV store, or specify a path like `"services/+/tyk/*"` for specific sub-paths.
- `authType`: Either `oidc` (for groups/users) or `k8s` (for service accounts).

**OIDC Specific Fields (`oidc-access.yaml`):**
- `dn`: The Distinguished Name (DN) of the group or entity. (For easy find, type: `go/whoami`).
- `capabilities`: 
  - List `- read` and `- write` for full access.
  - List only `- read` for read-only access.

**K8s Specific Fields (`k8s-access.yaml`):**
- `name`: The Kubernetes service account name.
- `namespace`: The namespace where the service account operates.
- `cluster`: The OpenShift cluster suffix (e.g., `dev`, `rak`, `jon`).
- `capabilities`: 
  - List `- read` and `- write` for full access.
  - List only `- read` for read-only access.

Notes:
 - Ensure that the configuration files are correctly formatted YAML before submitting the pull request.
 - Each KV store should have its own folder under `KVs/<Team-Name>/`.
 - If you need help setting up or troubleshooting, the your-team-name (e.g., cloudops) team is available to assist.
