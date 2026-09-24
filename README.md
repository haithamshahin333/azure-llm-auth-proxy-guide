# LangSmith LLM auth proxy → Azure AI Foundry

**Let self-hosted LangSmith call Azure AI Foundry (Azure OpenAI) models without an API key.** A Microsoft Entra identity you control
authenticates to Azure, Azure RBAC decides what it may call, and every model call is logged with the LangSmith workspace,
user and feature that made it.

This repository is the complete recipe for AKS: a handful of files, no scripts. You build one small container image, add one values
file to your LangSmith Helm release, install one upstream Helm chart, and finish three settings in the LangSmith UI.

```
README.md                        this guide
authz/authz.py · authz/Dockerfile   the sidecar that obtains Azure tokens (Python standard library only)
values-workload-identity.yaml    proxy values for path A, managed identity (default)
values-client-credentials.yaml   proxy values for path B, client id + secret
langsmith-values.yaml            values overlay for your LangSmith release
.gitignore                       keeps the signing key and secret files out of git
```

The kit covers every feature that calls a model: the **Playground, evaluators (LLM-as-judge), Fleet agents, Chat and
Insights**. It is the Azure path through the official page
[Set up the LLM auth proxy](https://docs.langchain.com/langsmith/llm-auth-proxy-self-hosted); generic topics (SaaS,
corporate proxies, body transformation, the full chart reference) are linked there rather than repeated.

## Contents

1. [Why this exists](#why-this-exists)
2. [How it works](#how-it-works)
3. [Two ways the proxy authenticates to Azure](#two-ways-the-proxy-authenticates-to-azure)
4. [URLs and paths: what goes where](#urls-and-paths-what-goes-where)
5. [What you need](#what-you-need)
6. [The install at a glance](#the-install-at-a-glance)
7. [Step by step](#step-by-step)
8. [Reading the proxy log](#reading-the-proxy-log)
9. [Reference: every LangSmith setting and why](#reference-every-langsmith-setting-and-why)
10. [Operations](#operations) · [Security notes](#security-notes) · [Troubleshooting](#troubleshooting) · [Teardown](#teardown)
11. [Not included](#not-included) · [Tested with](#tested-with) · [Alternative without a proxy](#alternative-without-a-proxy) · [Possible extensions](#possible-extensions)

## Why this exists

Several LangSmith features call a model on your behalf: the **Playground**, **evaluators** that score your traces with a
model, **Fleet** agents, **Chat** and **Insights** reports. Normally LangSmith holds an API key for the model provider. On
Azure that is often unwelcome: Foundry accounts are deployed with key-based (local) authentication disabled, a stored key is a shared
credential that cannot say who used it, and security teams want the call made by an identity they govern in Entra and RBAC.

LangSmith's **LLM auth proxy** feature (self-hosted, Enterprise plan) removes the key. LangSmith signs each model
request with a short-lived token that says *who* is calling; a small proxy you run in the cluster verifies that token and
swaps it for a real Azure credential. LangSmith never holds an Azure secret, Azure sees one identity you control, and the
proxy logs every call. LangChain publishes the proxy as the Helm chart `langchain/langsmith-auth-proxy` (Envoy with the
token check built in). The chart does not know how to talk to Azure; this kit supplies that piece and the exact LangSmith
settings. Nothing here is a fork.

## How it works

```
  YOUR KUBERNETES CLUSTER                                                             AZURE

  ┌─ LangSmith ─────────────────────┐       ┌─ Proxy pod: llm-auth-proxy ───────────┐
  │                                 │       │                                       │
  │  Playground, Evaluators, Fleet, │  (1)  │  envoy                                │
  │  Chat, Insights call models     ├──────►│  · checks the LangSmith signature,    │
  │                                 │       │    issuer and audience                │      ┌─ Microsoft Entra ID ──┐
  │  platform-backend               │       │  · asks authz for the Azure credential│ (2)  │                       │
  │  · signs every call             │       │                                       ├─────►│  issues an access     │
  │    with the private key         │◄──────┤  authz (sidecar)                      │◄─────┤  token to the proxy's │
  │  · publishes the public key at  │ reads │  · gets one Azure token, caches it    │      │  identity             │
  │    /.well-known/jwks.json       │ public│  · logs who called (attribution)      │      └───────────────────────┘
  │                                 │ key   │                                       │
  └─────────────────────────────────┘       │  envoy forwards the call to the       │ (3)  ┌─ UPSTREAM ────────────┐
                                            │  upstream with                        ├─────►│  Azure AI Foundry     │
                                            │  Authorization: Bearer <token>        │      │  (or a gateway in     │
                                            └───────────────────────────────────────┘      │  front of it)         │
                                                                                           └───────────────────────┘
  (1) http inside the cluster; header x-langsmith-llm-auth = a token signed by LangSmith, valid 5 minutes,
      naming the workspace, user and feature
  (2) path A: managed identity through AKS workload identity (no secret anywhere) · path B: client id + secret
  (3) https to the one host you configure (authProxy.upstream); the LangSmith token and the placeholder API key
      are stripped and never leave the cluster; Foundry authorizes the identity by RBAC
```

**What trusts what**, and which step creates it:

| Trust | Established by | Step |
|---|---|---|
| Proxy trusts LangSmith: "this call comes from LangSmith, from workspace X and user Y" | an Ed25519 signing key: private half in the LangSmith secret, public half published by LangSmith and read by Envoy | 2, 3 |
| LangSmith attaches the signed token to this endpoint | the organization setting (feature on, audience, allowed URLs) and a model configuration whose endpoint is the proxy | 3, 6 |
| Entra trusts the proxy: "this pod is identity Z" | path A: a federated credential on the managed identity naming the proxy's ServiceAccount · path B: the client secret | 1 |
| Foundry trusts identity Z: "it may call this model" | the RBAC role Cognitive Services OpenAI User on the Foundry account | 1 |

One request: a user runs a prompt → LangSmith signs a token naming workspace, user and feature and adds it as the header
`x-langsmith-llm-auth` → Envoy verifies it against LangSmith's published public key (fetched and cached for 5 minutes) and
refuses anything else → Envoy asks the sidecar for the Azure credential; the sidecar returns a cached Entra token for the
Foundry scope and logs who is calling → Envoy replaces the headers, rewrites the host to your Foundry account and forwards
over https → Foundry authorizes the identity and answers. If anything fails, the call fails; nothing falls back to a key.

Terms: **JWT** a signed token carrying claims · **JWKS** the published public key(s) that verify it · **issuer / audience**
two claims the proxy checks so a token meant for another system is refused · **ClusterIP Service** a DNS name reachable
only inside the cluster · **workload identity** the AKS feature that lets a pod act as a managed identity with no stored
secret · **federated credential** the rule on that identity naming which Kubernetes ServiceAccount may use it ·
**client credentials** the OAuth2 grant where an application authenticates with a client id and secret ·
**ext_authz** Envoy's hook that asks an external service whether to allow a request and which headers to add.

`authz/authz.py` is the official sample `ext_authz` service
([e2e/oauth](https://github.com/langchain-ai/helm/tree/main/charts/langsmith-auth-proxy/e2e/oauth) in the chart repository)
extended with workload identity, a readiness gate, stale-token serving and the attribution log line.

## Two ways the proxy authenticates to Azure

Pick one; everything else in this guide is identical.

| | **Path A: managed identity** (default) | **Path B: client credentials** |
|---|---|---|
| Azure identity | a user-assigned managed identity | an app registration (service principal) you own, or a client of a gateway in front of Foundry |
| How the pod proves who it is | AKS workload identity: Kubernetes issues the pod a token for its ServiceAccount, a federated credential on the identity trusts it, Entra exchanges it for an access token | the sidecar posts client id + client secret to the Entra token endpoint of your tenant, or to any https token endpoint you name |
| Secrets | **none** | one client secret, kept in a Kubernetes Secret in the proxy namespace |
| Cluster requirement | AKS with OIDC issuer and workload identity enabled | none |
| Values file | `values-workload-identity.yaml` | `values-client-credentials.yaml` |
| Rotation | nothing to rotate | Entra client secrets expire: reset, update the Secret, restart the pod |

Choose **A** whenever you can. Choose **B** when the cluster is not AKS or has no OIDC issuer, when identities are handed
out as app registrations, or when Foundry sits behind API Management or another identity provider that issues its own
tokens. The sidecar detects the path from its environment.

**Where the request goes.** The proxy forwards to the one host in `authProxy.upstream`: normally your Foundry account
(`https://<foundry-account>.openai.azure.com`). If a gateway sits in front of the model provider, set `upstream` to the
gateway URL and make the sidecar mint the token the gateway validates: path A with `TOKEN_SCOPE` set to the gateway's
application scope, or path B against the gateway's own token endpoint. LangSmith is configured the same way in every
case, because it only ever talks to the proxy. How the path is assembled is the next section.

## URLs and paths: what goes where

Three configuration points contribute to a model call's URL: the model configuration's Base URL, the client library, and
the proxy's `upstream`. Keep each piece in exactly one of them. (`<namespace>` is where the proxy is installed.)

| Piece | Example | Configured in |
|---|---|---|
| proxy address | `http://llm-auth-proxy.<namespace>.svc.cluster.local:10000` | model configuration **Base URL**, host part |
| provider path prefix | `/openai/v1` | model configuration **Base URL**, path part |
| operation | `/responses`, `/chat/completions`, `/embeddings` | appended by the client library; nothing to configure |
| upstream scheme and host | `https://<foundry-account>.openai.azure.com` | `authProxy.upstream` in the proxy values |
| gateway base path, only if you have one | `/azure` | `authProxy.upstream` too: `https://<gateway-host>/azure` |
| query string | `?api-version=…` (Azure OpenAI provider only) | added by the client library; passed through unchanged |

The proxy swaps the address and keeps the path. It replaces scheme, host and port with `upstream`, puts `upstream`'s base
path (if any) in front, rewrites `Host` and TLS SNI to the upstream host, and forwards path and query string unchanged.
A trailing slash on `upstream` makes no difference.

| Setup | `authProxy.upstream` | Base URL in the model configuration | What the upstream receives |
|---|---|---|---|
| Foundry directly (this kit's default) | `https://<foundry-account>.openai.azure.com` | `http://llm-auth-proxy.<namespace>.svc.cluster.local:10000/openai/v1` | `https://<foundry-account>.openai.azure.com/openai/v1/responses` |
| Gateway that mounts Foundry under a base path | `https://<gateway-host>/azure` | unchanged | `https://<gateway-host>/azure/openai/v1/responses` |
| Gateway with an OpenAI-compatible API at `/v1` | `https://<gateway-host>` | `http://llm-auth-proxy.<namespace>.svc.cluster.local:10000/v1` | `https://<gateway-host>/v1/chat/completions` |
| Azure OpenAI provider instead of OpenAI | same as the first row | the proxy address with **no path** | the Azure client builds `/openai/…?api-version=…` itself |

Rules of thumb: the Base URL path is what the upstream expects in front of the operation, minus anything already in
`upstream`; never put the same segment in both (the upstream sees `/openai/v1/openai/v1/…` and answers 404); one proxy
install has one `upstream`, so one Foundry account or gateway per install.

## What you need

| Requirement | Detail |
|---|---|
| LangSmith | Self-hosted **0.16 or later** (chart and application versions move together; the values keys this kit sets exist from 0.16, the feature itself since 0.13.39), **Enterprise plan**. Installed with Helm, the LangChain Terraform module or GitOps: this kit only adds one values file to your existing upgrade. |
| Kubernetes | Path A: AKS with OIDC issuer and workload identity enabled (`az aks update -n <aks> -g <rg> --enable-oidc-issuer --enable-workload-identity`). Path B: any cluster. Install the proxy **in the LangSmith namespace** unless you have a reason not to (a separate namespace needs one NetworkPolicy, see Operations). |
| Azure AI Foundry | An account (kind `AIServices` or `OpenAI`) with a model deployment. Keys may stay disabled. Give the deployment enough throughput: a Fleet agent turn carries a large system prompt and tool schemas, and a small deployment (for example 10K tokens per minute) answers 429 to Fleet calls. |
| Your Azure permissions | Path A: create a managed identity in the resource group (Contributor). Both paths: assign a role on the Foundry account (Owner or User Access Administrator), or have an admin run that one command. |
| A container registry | The cluster must be able to pull the sidecar image you build (AKS: `az aks update -n <aks> -g <rg> --attach-acr <registry>`). No registry: see Operations. |
| Tools | `az` (logged in), `kubectl` (pointed at the cluster), `helm` 3, `docker`, `jq`, and `step` or `openssl` for the signing key. `python3` with `pip` only for the optional end-to-end test in step 7. |
| Who does what | Steps 1, 4, 5: Azure and cluster admin. Steps 2, 3: whoever runs the LangSmith Helm upgrades. Step 6: a LangSmith **organization admin** in the UI. Step 7: anyone with cluster access. |

## The install at a glance

| Side | Step | What happens | How |
|---|---|---|---|
| Azure | 1 | the identity the proxy uses, with the inference role on Foundry | a few `az` commands (A), or one `az` + one `kubectl` (B) |
| LangSmith | 2 | a signing key so LangSmith can sign requests | a few `step` or `openssl` commands |
| LangSmith | 3 | LangSmith learns the issuer, gets the key, and is allowed to call an http Service inside the cluster | `helm upgrade` of **your** release with `langsmith-values.yaml` |
| Proxy | 4 | the sidecar image | `docker build` + `docker push` |
| Proxy | 5 | Envoy + sidecar as one pod, Service `llm-auth-proxy` | `helm upgrade --install` of the upstream chart with one values file |
| LangSmith | 6 | feature on for the organization, a placeholder secret, a model configuration pointing at the proxy | three screens in the UI |
| Both | 7 | prove it | a test call, a Playground prompt, the proxy log |

Three values must match on both sides: the **issuer** (`langsmith:self-hosted:<yours>` in `langsmith-values.yaml` and in
the proxy values), the **audience** (`llm-auth-proxy` in the proxy values and in the organization setting), and the **proxy
address** `llm-auth-proxy.<namespace>.svc.cluster.local` (in the model configuration). `<yours>` is any short name for
this installation, for example `langsmith:self-hosted:acme-prod`; it only has to be identical on both sides. `<namespace>`
is where you install the proxy; `<langsmith-namespace>` and `<langsmith-release>` describe your existing LangSmith
installation (usually both `langsmith`).

## Step by step

Run every command from the root of this repository, the folder holding this README.

### Step 1 (Azure): the identity the proxy uses

**Path A: managed identity.** Create it, trust the proxy's ServiceAccount (the namespace you will install into plus the
fixed name `llm-auth-proxy`), and grant it the inference role on the Foundry account.

```bash
az identity create -n <identity> -g <rg>
ISSUER=$(az aks show -n <aks> -g <rg> --query oidcIssuerProfile.issuerUrl -o tsv)
az identity federated-credential create --name aks-llm-auth-proxy --identity-name <identity> -g <rg> \
  --issuer "$ISSUER" --subject system:serviceaccount:<namespace>:llm-auth-proxy --audiences api://AzureADTokenExchange
az role assignment create --role "Cognitive Services OpenAI User" --assignee-principal-type ServicePrincipal \
  --assignee-object-id $(az identity show -n <identity> -g <rg> --query principalId -o tsv) \
  --scope $(az cognitiveservices account show -n <foundry-account> -g <rg> --query id -o tsv)
```
Note two values for step 5: `az identity show -n <identity> -g <rg> --query clientId -o tsv` and
`az account show --query tenantId -o tsv`. A new principal can take a few minutes to become visible to RBAC.

**Path B: client credentials.** You own the app registration (or the gateway client). Grant its service principal the
same role and put the client secret in a Kubernetes Secret, through a file rather than a command-line argument so the
secret never lands in your shell history:

```bash
az role assignment create --role "Cognitive Services OpenAI User" --assignee-principal-type ServicePrincipal \
  --assignee-object-id $(az ad sp show --id <client-id> --query id -o tsv) \
  --scope $(az cognitiveservices account show -n <foundry-account> -g <rg> --query id -o tsv)
kubectl create namespace <namespace>        # only if the proxy gets its own namespace
kubectl create secret generic llm-auth-proxy-oauth -n <namespace> --from-file=OAUTH_CLIENT_SECRET=<secret-file>   # a file holding only the secret, chmod 600
```
Need an app registration first? `az ad app create --display-name langsmith-llm-auth-proxy --query appId -o tsv` gives
`<client-id>`; then `az ad sp create --id <client-id>` and
`az ad app credential reset --id <client-id> --append --years 1 --query password -o tsv` (shown once; note the expiry).

### Step 2 (LangSmith): a signing key

LangSmith signs each request with a private Ed25519 key and serves the public half at `/.well-known/jwks.json`. Generate
one with the [step CLI](https://smallstep.com/docs/step-cli/installation/), as on the official page:

```bash
step crypto keypair pub.pem priv.pem --kty OKP --crv Ed25519 --no-password --insecure   # --insecure only means: no passphrase on the file
step crypto key format --jwk --no-password --insecure < priv.pem | jq -c '{keys: [. + {use: "sig", alg: "EdDSA"}]}' > signing-jwks.json
rm pub.pem priv.pem
```
or with OpenSSL 1.1.1 or later and `jq`:
```bash
openssl genpkey -algorithm ed25519 -out priv.pem
D=$(openssl pkey -in priv.pem -outform DER | tail -c 32 | base64 | tr -d '\n=' | tr '+/' '-_')
X=$(openssl pkey -in priv.pem -pubout -outform DER | tail -c 32 | base64 | tr -d '\n=' | tr '+/' '-_')
KID=$(openssl pkey -in priv.pem -pubout -outform DER | tail -c 32 | openssl dgst -sha256 | awk '{print $NF}' | cut -c1-16)
jq -nc --arg x "$X" --arg d "$D" --arg kid "$KID" '{keys:[{kty:"OKP",crv:"Ed25519",x:$x,d:$d,use:"sig",alg:"EdDSA",kid:$kid}]}' > signing-jwks.json
rm priv.pem
```
`signing-jwks.json` is the **private** key: mode 0600, never in git. The same key also signs other LangSmith-issued
tokens, so keep it stable.

The official page instead creates a separate secret holding `LANGSMITH_SIGNING_JWKS` and references it with `secretRef`
on platform-backend. That works too; this kit uses the chart's built-in key so no extra secret is needed.

### Step 3 (LangSmith): the values overlay

This step has the most moving parts, so here is exactly what LangSmith needs and why. The proxy is reached at
`http://llm-auth-proxy.<namespace>.svc.cluster.local:10000`: plain http, a `*.svc.cluster.local` name, a private ClusterIP.
LangSmith blocks each of those by default and gates the feature per organization. The kit's `langsmith-values.yaml` lifts
exactly those blocks, pod by pod. This is its content, block by block, with the reason for each. Use the file as it is
with `-f`, or copy the blocks into your own values.

**1. Issuer, every pod.** The `iss` claim LangSmith writes into each token. The proxy refuses any other issuer, so this
string must equal `jwtIssuer` in the proxy values (step 5). It is the only placeholder in the file.

```yaml
config:
  llmAuthProxyIssuer: "langsmith:self-hosted:<yours>"   # becomes LLM_AUTH_PROXY_ISSUER on every pod
```

**2. Entitlement: backend, platform-backend, queue.** Grants every organization the feature (Personal organizations
excepted). Without it the organization setting in step 6 answers 403. To entitle a single organization instead, run the
SQL statement in [step 2 of the official page](https://docs.langchain.com/langsmith/llm-auth-proxy-self-hosted#2-enable-llm-auth-proxy-for-your-organization) and leave this block out.

```yaml
backend:
  deployment:
    extraEnv:
      - name: DEFAULT_ORG_FEATURE_CAN_USE_LLM_AUTH_PROXY
        value: "true"
platformBackend:
  deployment:
    extraEnv:
      - name: DEFAULT_ORG_FEATURE_CAN_USE_LLM_AUTH_PROXY
        value: "true"
queue:
  deployment:
    extraEnv:
      - name: DEFAULT_ORG_FEATURE_CAN_USE_LLM_AUTH_PROXY
        value: "true"
```

**3. Playground pod.** This pod makes the Playground **and** the evaluator (LLM-as-judge) model calls. The three variables
allow a cluster-internal, private-IP, plain-http model endpoint.

```yaml
playground:
  deployment:
    extraEnv:
      - name: SSRF_ALLOW_K8S_INTERNAL             # *.svc.cluster.local hosts
        value: "true"
      - name: SSRF_ALLOW_PRIVATE_IPS_PLAYGROUND   # the Service ClusterIP is a private address
        value: "true"
      - name: LLM_AUTH_PROXY_ACCEPT_HTTP          # otherwise the token is attached to https URLs only
        value: "true"
```

**4. Add-ons: Fleet, Chat (Polly), Insights.** Their api-server and queue pods have the same http gate and need
`LLM_AUTH_PROXY_ACCEPT_HTTP` only: Chat and Insights have no SSRF check, and the chart already opens Fleet's. A block is
ignored when that add-on is disabled, so the one file serves every installation. Two traps: do **not** add the `SSRF_*`
variables to the Fleet pods (the chart sets them there, and a second copy fails the render with `Duplicate keys detected`),
and use these keys exactly. The chart has no values schema, so a block under another key (for example
`config.polly.agent.extraEnv`) is accepted silently and the variable never reaches the pod.

```yaml
fleet:
  apiServer:
    deployment:
      extraEnv:
        - name: LLM_AUTH_PROXY_ACCEPT_HTTP
          value: "true"
  queue:
    deployment:
      extraEnv:
        - name: LLM_AUTH_PROXY_ACCEPT_HTTP
          value: "true"
polly:                                            # Chat
  apiServer:
    deployment:
      extraEnv:
        - name: LLM_AUTH_PROXY_ACCEPT_HTTP
          value: "true"
  queue:
    deployment:
      extraEnv:
        - name: LLM_AUTH_PROXY_ACCEPT_HTTP
          value: "true"
engineInsightsAgent:                              # Insights
  apiServer:
    deployment:
      extraEnv:
        - name: LLM_AUTH_PROXY_ACCEPT_HTTP
          value: "true"
  queue:
    deployment:
      extraEnv:
        - name: LLM_AUTH_PROXY_ACCEPT_HTTP
          value: "true"
```

None of this goes into `commonEnv`: the playground pod does not receive it, and the Fleet pods would get duplicates.

**Apply it.** Edit the one placeholder, then upgrade your release with the kit's file **after** your own values, plus the
key from step 2:

```bash
helm upgrade <langsmith-release> langchain/langsmith --version <your chart version> -n <langsmith-namespace> \
  -f <your values...> -f langsmith-values.yaml --set-file config.signingJwks=./signing-jwks.json
```

- **Your release uses `config.existingSecretName`** (you manage the LangSmith secret): skip `--set-file` and add the key
  `langsmith_signing_jwks` with the file's content to that secret at its source (Key Vault + External Secrets, the
  Terraform module's secrets input, a sealed secret, or `kubectl patch`). Then restart platform-backend.
- **Your values already set `extraEnv` on any of these pods**: Helm replaces a list, it does not merge it. Add the kit's
  entries to your own list for that pod instead of adding the file, for example:
  ```yaml
  playground:
    deployment:
      extraEnv:
        - name: <your existing variable>
          value: "<its value>"
        - name: SSRF_ALLOW_K8S_INTERNAL
          value: "true"
        - name: SSRF_ALLOW_PRIVATE_IPS_PLAYGROUND
          value: "true"
        - name: LLM_AUTH_PROXY_ACCEPT_HTTP
          value: "true"
  ```
- **GitOps**: commit the blocks above and the key reference into your values source.

platform-backend, backend, queue and playground restart once, plus the add-on pods you have enabled. In-flight
Playground and evaluator calls fail during that rollout; traces and the UI are unaffected.

**Check** that the key is served and the issuer landed:
```bash
kubectl run jwks-check -n <langsmith-namespace> --rm -i --restart=Never --image=curlimages/curl:8.11.1 --quiet -- \
  curl -s http://<langsmith-release>-frontend.<langsmith-namespace>.svc.cluster.local/.well-known/jwks.json
kubectl get cm <langsmith-release>-config -n <langsmith-namespace> -o jsonpath='{.data.LLM_AUTH_PROXY_ISSUER}'; echo
```
The first prints `{"keys":[{"kty":"OKP","crv":"Ed25519",…}]}` with no `d` field (an empty key list means platform-backend
started before the key existed: restart it). The second prints your issuer.

### Step 4 (proxy): the sidecar image

```bash
docker build --platform linux/amd64 -t <registry>/langsmith-llm-authz:1 authz/
docker push <registry>/langsmith-llm-authz:1
```
`python:3.12-slim` plus one file; nothing is installed. Rebuild only when `authz/authz.py` changes.

### Step 5 (proxy): install the chart

**What you are installing.** The proxy itself is not part of this kit. LangChain publishes it as the Helm chart
`langsmith-auth-proxy` in the same Helm repository as LangSmith
([source and README](https://github.com/langchain-ai/helm/tree/main/charts/langsmith-auth-proxy)). In Helm terms: the
*chart* is a template for Kubernetes objects, a *values file* fills in that template, and a *release* is one installed copy.
This kit provides only the values file. With it, chart 0.0.15 creates four objects in `<namespace>`, all named
`llm-auth-proxy`:

| Object | What it is |
|---|---|
| Deployment | one pod with two containers: `envoy` (the chart's own, image `envoyproxy/envoy:v1.37-latest`, listening on 10000) and `authz` (the sidecar from step 4, on `localhost:10002`) |
| ConfigMap | the Envoy configuration the chart generates from your values: token check, `ext_authz` call to the sidecar, route to `upstream` |
| Service | `llm-auth-proxy:10000`, the address LangSmith calls |
| ServiceAccount | the pod's identity; path A annotates it for workload identity |

The values file has five parts, top to bottom: `fullnameOverride: llm` so every object is called `llm-auth-proxy` (the
chart appends `-auth-proxy`); the
`authProxy` settings the chart was built for (upstream, issuer, audience, JWKS address, `extAuthz` pointing at
`http://localhost:10002`); the ServiceAccount annotations and pod label for workload identity (path A only); the sidecar
container under `authProxy.deployment.sidecars` (image, env, port, readiness probe, resources), which is how the chart lets
you add your own container to its pod; and `ingress`/`gateway` off because LangSmith reaches the Service directly.
Everything not in the file keeps the chart's default. To add the repository, see the defaults, or preview exactly what
will be created:

```bash
helm repo add langchain https://langchain-ai.github.io/helm/ && helm repo update          # once per machine
helm show values langchain/langsmith-auth-proxy --version 0.0.15                            # every option, with defaults
helm template llm-auth-proxy langchain/langsmith-auth-proxy --version 0.0.15 -n <namespace> -f values-workload-identity.yaml
```

The [chart README](https://github.com/langchain-ai/helm/tree/main/charts/langsmith-auth-proxy#readme) documents every
option. To move to a newer chart version later, change `--version`, run `helm template` again to compare, then run the
same install command below.

Copy **one** values file (`values-workload-identity.yaml` for path A, `values-client-credentials.yaml` for path B) and fill
in the placeholders; each has a comment saying where the value comes from:

| Placeholder | Value | Path |
|---|---|---|
| `<registry>/langsmith-llm-authz:1` | the image from step 4 | both |
| `<foundry-account>` | your Foundry account name (`upstream` = `https://<foundry-account>.openai.azure.com`; a gateway URL instead if one fronts Foundry) | both |
| `langsmith:self-hosted:<yours>` | the issuer from step 3, exactly | both |
| `jwtAudiences: ["llm-auth-proxy"]` | keep, or change together with the organization setting in step 6 | both |
| `<langsmith-release>-frontend.<langsmith-namespace>` | the LangSmith frontend Service (`kubectl get svc -n <langsmith-namespace> | grep frontend`) | both |
| `<identity-client-id>`, `<tenant-id>` | from step 1 | A |
| `<tenant-id>` in `OAUTH_TOKEN_URL`, `<client-id>` | from step 1; `OAUTH_TOKEN_URL` may also be a gateway's token endpoint | B |

```bash
helm upgrade --install llm-auth-proxy langchain/langsmith-auth-proxy --version 0.0.15 \
  -n <namespace> --create-namespace -f values-workload-identity.yaml --wait --timeout 3m   # path B: values-client-credentials.yaml
```

The command is its own check. The pod becomes **Ready only after the sidecar has obtained an Azure token** (its readiness
probe calls the token endpoint; cached, so one real request per token lifetime). If the identity, federated credential,
role or client secret is wrong, `--wait` fails with `Deployment … not ready`, and the sidecar log (next section) shows the
Entra error code. Fix and run the same command again. On success `kubectl get pods -n <namespace>` shows
`llm-auth-proxy-… 2/2 Running`.

### Step 6 (LangSmith UI): turn it on and point a model configuration at it

As an **organization admin**, Settings → General → **LLM auth proxy**:

1. Tick *Enable LLM auth proxy*. *JWT audience* = the `jwtAudiences` value from step 5 (`llm-auth-proxy` unless you
   changed it). *Allowed URLs*: **Allow all**. This means LangSmith attaches its signed token to every model call its
   proxy-aware features make, whatever host the configuration points at; the token is only valid for your proxy's
   audience and expires after five minutes. *Custom* restricts that to listed hostnames (enter
   `llm-auth-proxy.<namespace>.svc.cluster.local`, no scheme or port), but in current LangSmith versions Chat does not
   attach the token with a custom list, so use *Allow all* if Chat should use the proxy. *Block all* disables the feature.
   A 403 on save means the step 3 upgrade has not finished rolling the backend pods.

2. **Workspace secret**: Settings → your workspace → Secrets → `OPENAI_API_KEY` = `llm-auth-proxy`. **Why a placeholder is needed:** the
   OpenAI client library refuses to start without an API key value, and LangSmith requires a model configuration to name
   a secret. The value itself is never used: the proxy replaces the `Authorization` header with the real Azure token and
   strips `api-key`, so Azure never sees it. Any non-empty string works.

3. **Model configuration**: Playground → model configurations → New. Use provider **OpenAI**, not Azure OpenAI, even
   though the model runs on Foundry: Azure exposes the OpenAI-compatible v1 API, the configuration needs no API version,
   and it is the combination every LangSmith feature supports through the proxy (Chat requires it).

   | Field | Value |
   |---|---|
   | Provider | OpenAI |
   | Model name | your Foundry **deployment name** (for example `gpt-4.1-mini`); Azure's v1 API takes it as `model` |
   | Provider API | Responses or Chat Completions, both work |
   | Base URL | `http://llm-auth-proxy.<namespace>.svc.cluster.local:10000/openai/v1` |
   | API key secret | `OPENAI_API_KEY` (the placeholder) |
   | Feature Access | tick the features that should use it: Playground, Evaluators, Fleet, Chat, and the two Insights rows (Thinking, Summarization) |

   Insights picks a model per row. Select this configuration in **both** Insights rows; a row left on a built-in model
   calls that provider directly, not through the proxy.

   The Base URL is the proxy's Service address plus `/openai/v1`: the OpenAI client appends `/responses` or
   `/chat/completions`, and Azure serves those under `/openai/v1/` (see [URLs and paths](#urls-and-paths-what-goes-where)).
   A wrong model name comes back as Azure's `DeploymentNotFound`; a Base URL without `/openai/v1` as `404 Resource not found`.

   The *Azure OpenAI* provider also works for the Playground, evaluators and Fleet (endpoint = the proxy address with no
   path, API version `2025-03-01-preview` or later because the Playground uses the Responses API, secret
   `AZURE_OPENAI_API_KEY`), but not for Chat in current versions.

### Step 7: prove it

`kubectl get pods -n <namespace>` showing `2/2 Running` already proves a token was obtained. The rest runs from your
machine through a port-forward (`kill %1` closes it when you are done).

**Envoy refuses a bogus token** before anything else happens:
```bash
kubectl port-forward -n <namespace> svc/llm-auth-proxy 10000:10000 &
curl -s -o /dev/null -w '%{http_code}\n' -H 'x-langsmith-llm-auth: bogus' 'http://localhost:10000/openai/v1/responses'   # 401
```

**The whole chain without LangSmith** (optional; needs `python3` and `pip install pyjwt cryptography`): sign a test token
with the key from step 2 and send it through the proxy. A completion from Foundry proves Envoy, the key fetch, the sidecar,
the identity and the role:
```bash
JWT=$(python3 - <<'PY'
import json, time, uuid, jwt
from jwt.algorithms import OKPAlgorithm
k = json.load(open("signing-jwks.json"))["keys"][0]; now = int(time.time())
claims = {"iss": "langsmith:self-hosted:<yours>", "aud": ["llm-auth-proxy"], "sub": "smoke", "iat": now, "exp": now + 300,
          "jti": str(uuid.uuid4()), "workspace_id": "smoke", "workspace_name": "smoke", "actor_type": "user"}
print(jwt.encode(claims, OKPAlgorithm.from_jwk(json.dumps(k)), algorithm="EdDSA", headers={"kid": k.get("kid")}))
PY
)
curl -s -H "x-langsmith-llm-auth: $JWT" -H 'content-type: application/json' \
  -d '{"model":"<deployment-name>","input":"Reply with the single word pong."}' 'http://localhost:10000/openai/v1/responses'
```

**From LangSmith**: a Playground prompt on the new configuration, a Chat message, a Fleet agent turn, an Insights report,
then read the log.

## Reading the proxy log

The sidecar prints one JSON line per model call. Check it first when anything fails.

```bash
kubectl logs -n <namespace> deploy/llm-auth-proxy -c authz -f
```

```json
{"msg": "llm-authz listening on :10002", "mode": "workload-identity", "client_id": "<identity-client-id>", "token_url": "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token", "scope": "https://cognitiveservices.azure.com/.default", "client_auth": "assertion", "log_headers": true}
{"msg": "token minted", "mint_ms": 557, "expires_in": 86398}
{"ts": 1700000000.0, "status": 200, "method": "POST", "path": "/openai/v1/responses", "authority": "llm-auth-proxy.<namespace>.svc.cluster.local:10000", "envoy_request_id": "3f9a…", "token": "cache", "organization_id": "e5f6…", "organization_name": "my-org", "workspace_id": "a1b2…", "workspace_name": "my-workspace", "ls_user_id": "c3d4…", "actor_type": "user", "sub": "c3d4…", "request_id": "…", "headers": {"host": "llm-auth-proxy.<namespace>.svc.cluster.local:10000", "content-length": "412", "content-type": "application/json", "authorization": "<redacted, 21 chars>", "x-forwarded-proto": "http", "x-request-id": "3f9a…", "user-agent": "OpenAI/Python …", "accept": "application/json", "x-langsmith-llm-auth": "<redacted, 516 chars>", "x-envoy-internal": "true", "x-envoy-expected-rq-timeout-ms": "10000"}, "claims": {"iss": "langsmith:self-hosted:<yours>", "aud": "llm-auth-proxy", "sub": "c3d4…", "iat": 1700000000, "exp": 1700000300, "jti": "…", "actor_type": "user", "organization_id": "e5f6…", "organization_name": "my-org", "workspace_id": "a1b2…", "workspace_name": "my-workspace", "ls_user_id": "c3d4…", "request_id": "…"}}
```

| Field | Meaning |
|---|---|
| `status` | the sidecar's answer: `200` token supplied, `503` it could not obtain a token (the reason is in an `error` line just before) |
| `method`, `path` | what LangSmith requested; a Base URL without `/openai/v1` or a wrong API version shows up here |
| `authority` | the host LangSmith addressed |
| `token` | `cache` (normal), `minted` (a fresh token was obtained for this call), `stale` (a refresh failed and the still-valid cached token was used) |
| `actor_type` | `user` Playground, `evaluator` LLM-as-judge (`sub` is the evaluator id), `agent-builder` Fleet, under its internal name (`sub` is the agent id), `polly` Chat, `insights` Insights, `api_key:pat` or `api_key:service` for calls made with a LangSmith API key |
| `organization_*`, `workspace_*`, `ls_user_id` | who made the call. `ls_user_id` is the person; on Fleet runs `sub` is the assistant, not a person. `request_id` correlates with LangSmith traces, `envoy_request_id` with Envoy. All claims: [JWT claims reference](https://docs.langchain.com/langsmith/llm-auth-proxy-self-hosted#jwt-claims-reference) |
| `headers`, `claims` | every header Envoy forwarded and the full token. `authorization`, `api-key`, `x-api-key`, `x-langsmith-llm-auth`, `cookie` and `proxy-authorization` are reduced to their length; with the placeholder value `llm-auth-proxy`, `authorization` shows 21 chars. `user-agent` tells the client library apart, `x-forwarded-proto` shows http or https |

**Compact lines**: header and claim logging is on by default, about 1 KB per call. For the short form set
`LOG_HEADERS=false` in the sidecar's `env` (both values files carry the entry commented out) and restart the pod.
Credentials are never logged either way.

**What the log cannot show.** Calls Envoy refused before asking the sidecar, and what Foundry answered. A caller that gets
`401 Jwt is missing` sent no token (a LangSmith-side setting from steps 3 or 6 is missing); `401 Jwt verification fails`
means a wrong key or issuer; `403` means a wrong audience. Foundry errors (`DeploymentNotFound`, `PermissionDenied`, 429)
appear in the calling feature's error message. The chart's Envoy has no access log.

## Reference: every LangSmith setting and why

| Where | Setting | Why |
|---|---|---|
| every pod | `LLM_AUTH_PROXY_ISSUER` = the proxy's `jwtIssuer` | the `iss` claim; the proxy refuses any other |
| backend, platform-backend, queue | `DEFAULT_ORG_FEATURE_CAN_USE_LLM_AUTH_PROXY=true` | organization entitlement |
| playground | `SSRF_ALLOW_K8S_INTERNAL`, `SSRF_ALLOW_PRIVATE_IPS_PLAYGROUND`, `LLM_AUTH_PROXY_ACCEPT_HTTP` | cluster-internal, private-IP, plain-http endpoint allowed for Playground and evaluator calls |
| Fleet, Chat, Insights api-server and queue | `LLM_AUTH_PROXY_ACCEPT_HTTP=true` only | the same http gate; the chart already sets `SSRF_*` on the Fleet pods |
| Organization setting → Enable | on | LangSmith attaches the signed token at all |
| Organization setting → JWT audience | `llm-auth-proxy` | the `aud` claim; must equal `jwtAudiences` in the proxy values |
| Organization setting → Allowed URLs | Allow all (or Custom with the proxy hostname; Chat needs Allow all) | which hosts receive the token |
| Model configuration → Base URL | `http://llm-auth-proxy.<namespace>.svc.cluster.local:10000/openai/v1` | where the client sends the call |
| Workspace secret | `OPENAI_API_KEY` = any value | satisfies the client library; replaced by the proxy |

Behind a TLS ingress instead of the in-cluster address, the playground variables and the add-ons' `LLM_AUTH_PROXY_ACCEPT_HTTP`
are not needed and the allowed URL is the public host.

## Operations

- **Fail-closed.** When the sidecar cannot obtain a token it answers 503 and Envoy shows that to LangSmith as `403`; when
  Foundry rejects the token you see `401 PermissionDenied`. Nothing falls back to a key. The cause is in the sidecar log.
- **Kill switch**: untick *Enable LLM auth proxy* in the organization setting; proxy and configuration stay in place.
- **Rotate the signing key**: new `signing-jwks.json`, step 3 again (or update your secret and restart platform-backend).
  Envoy refetches the public key within `jwksCacheDurationSeconds` (300 s); calls signed by the old key fail until then.
- **Rotate the client secret** (path B): `az ad app credential reset --id <client-id> --append --years 1`, write the new
  value into the Secret (`kubectl create secret … --dry-run=client -o yaml | kubectl apply -f -`), then
  `kubectl rollout restart -n <namespace> deploy/llm-auth-proxy`.
- **Switch paths**: step 1 for the other path, then step 5 with the other values file. Nothing changes on the LangSmith side.
- **Proxy in another namespace than LangSmith**: the LangSmith chart ships `default-deny-ingress`, so Envoy's fetch of the
  public key needs this in the LangSmith namespace:
  ```yaml
  apiVersion: networking.k8s.io/v1
  kind: NetworkPolicy
  metadata: { name: llm-auth-proxy-jwks, namespace: <langsmith-namespace> }
  spec:
    podSelector:
      matchLabels: { app.kubernetes.io/instance: <langsmith-release>, app.kubernetes.io/component: langsmith-frontend }
    policyTypes: [Ingress]
    ingress:
      - from: [ { namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: <namespace> } } } ]
        ports: [ { port: frontend, protocol: TCP } ]
  ```
  If the proxy namespace denies ingress itself, also allow the LangSmith pods → `llm-auth-proxy:10000` (only that port:
  the sidecar's 10002 refuses token requests from other pods, but nothing needs to reach it). Egress the pod needs:
  Envoy → the LangSmith frontend `:80` (public key), Envoy → Foundry `:443`, sidecar → `login.microsoftonline.com:443`
  (or your token endpoint).
- **No registry**: ship the sidecar source as a ConfigMap: `kubectl create configmap llm-authz-src -n <namespace> --from-file=authz/authz.py`,
  then in the values file set `image: python:3.12-slim`, add `command: ["python", "/app/authz.py"]` and
  `volumeMounts: [{name: authz-src, mountPath: /app, readOnly: true}]` to the sidecar, and
  `volumes: [{name: authz-src, configMap: {name: llm-authz-src}}]` under `deployment`.
- **Capacity**: one replica by default; the official guidance for production is at least three
  (`authProxy.deployment.replicas`, or `authProxy.autoscaling.hpa` with `minReplicas: 3`). Each replica caches its own
  token and refreshes it before expiry.
- **Corporate egress proxy**: Envoy ignores `HTTP_PROXY`-style variables; configure `authProxy.httpProxy`
  ([HTTP proxy](https://docs.langchain.com/langsmith/llm-auth-proxy-self-hosted#http-proxy)). The sidecar honors the standard `HTTPS_PROXY` and `NO_PROXY` variables: add them to
  its `env` so the token endpoint is reached through the same proxy.
- **Private CA or mTLS towards the upstream** (a gateway with an internal certificate): the chart's `customCa` and `mtls`
  settings ([chart README](https://github.com/langchain-ai/helm/tree/main/charts/langsmith-auth-proxy#readme)).
- **Envoy cannot reach the LangSmith frontend at all**: paste the public key inline with `jwksJson` instead of `jwksUri`
  ([step 4 of the official page](https://docs.langchain.com/langsmith/llm-auth-proxy-self-hosted#4-install-the-auth-proxy-helm-chart)); a key rotation then needs a chart upgrade.
- **TLS instead of in-cluster http**: enable the chart's `ingress` with a certificate, point the model configuration at
  `https://<host>/openai/v1`, and drop the playground variables and the add-ons' `LLM_AUTH_PROXY_ACCEPT_HTTP` from the overlay.
- **One identity per workspace** (attribution enforced by Azure, not just logged): one managed identity per workspace,
  each with a federated credential for the same ServiceAccount and its own role assignment; in `authz.py` pick the client
  id from the `workspace_id` claim before minting. Path B: one client id + secret per workspace, likewise.

## Security notes

- The private signing key lives in your LangSmith secret (and in `signing-jwks.json` where you generated it). The proxy
  never sees it.
- LangSmith → proxy is plain http inside the cluster (token and prompts on the pod network): fine inside a private VNet,
  use the TLS variant for anything stricter. As of chart 0.0.15 the chart does not validate Foundry's TLS certificate (no
  `validation_context` in its Envoy config); it does set SNI and pin the host.
- The sidecar trusts Envoy's token check and decodes the claims unverified for the log line. It hands out a token only to
  requests from localhost, that is from Envoy in the same pod; a request arriving on the pod IP gets `403` and a
  `refused token request` log line. If you ever change that, verify the token in the sidecar too. Its per-call log line
  carries the forwarded headers with credentials redacted; set `LOG_HEADERS=false` if header values count as sensitive for
  you.
- Path B: the client secret lives in the Kubernetes Secret and the sidecar's environment. The sidecar never logs it or the
  tokens it obtains; token-endpoint failures are logged as the OAuth error code plus a truncated description.
- With *Allow all*, the signed token accompanies every model call LangSmith's proxy-aware features make. It is only
  accepted by your proxy (audience) and expires after five minutes.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| step 5 times out; sidecar log `AADSTS700016` | The identity or app is not in the tenant the token URL points at: check the `tenant-id` annotation (A) or `OAUTH_TOKEN_URL` (B). |
| sidecar log `AADSTS70021` / `AADSTS700213` | Federated credential mismatch: the subject must be `system:serviceaccount:<namespace>:llm-auth-proxy` with the cluster's current OIDC issuer. |
| sidecar log `invalid_client` / `AADSTS7000215` | Wrong or expired client secret: update the Secret and restart the pod. |
| sidecar log `AADSTS500011` / `invalid_scope` | `OAUTH_SCOPE` / `TOKEN_SCOPE` is not a resource this endpoint knows (Entra: `https://cognitiveservices.azure.com/.default`). |
| sidecar log `no credentials: neither …` | A: pod label `azure.workload.identity/use` or the ServiceAccount annotations missing, or the workload identity webhook is not installed. B: the Secret is not referenced. |
| pod `CreateContainerConfigError` | Secret `llm-auth-proxy-oauth` or its key `OAUTH_CLIENT_SECRET` does not exist in `<namespace>`. |
| pod `ImagePullBackOff` | The cluster cannot pull `<registry>/langsmith-llm-authz` (attach the registry) or `envoyproxy/envoy` from docker.io (set `images.registry` to a mirror). |
| `/.well-known/jwks.json` is empty | platform-backend started before the key existed: `kubectl rollout restart deploy/<langsmith-release>-platform-backend`. |
| organization setting: `403` on save | Entitlement not live: the step 3 upgrade did not roll backend and platform-backend, or another `-f` file replaced the `extraEnv` list. Or the user is not an organization admin. |
| Playground: `SSRF blocked: Kubernetes internal DNS` | Playground `extraEnv` missing (step 3). |
| Any feature: `401 Jwt is missing` | The call carried no token. Playground or evaluators: the playground variables from step 3. Fleet, Chat or Insights: `LLM_AUTH_PROXY_ACCEPT_HTTP` on their pods (Fleet also logs `rejected non-HTTPS URL`). Chat: also needs *Allow all* and the OpenAI provider (step 6). Any: the feature not enabled, or the configuration not created through the dialog. |
| Chat: `401 Jwt is missing` although *Allow all* and the OpenAI provider are set | This LangSmith version does not attach the token from Chat. Give Chat its own OAuth client-credentials configuration (see Alternative without a proxy) and report the version to LangChain support. |
| sidecar log `refused token request from <ip>` | Something other than Envoy in the pod called port 10002; the sidecar answered 403 without a token. Check the NetworkPolicy. |
| Any feature: `401 Jwt verification fails` | Issuer or key mismatch between `langsmith-values.yaml`/the LangSmith secret and the proxy values. |
| Any feature: `403` with a good token | Audience mismatch between the organization setting and `jwtAudiences`. |
| step 7 test: `401 … Jwks remote fetch is failed` | Envoy cannot reach the LangSmith frontend: wrong `jwksUri`, or NetworkPolicy/DNS between namespaces (Operations). |
| `404 Resource not found` from Foundry | Base URL without `/openai/v1` (OpenAI provider), or an API version older than `2025-03-01-preview` with the Azure OpenAI provider and the Responses API. |
| `DeploymentNotFound` from Foundry | The model name is not a deployment name on the account. |
| `401 PermissionDenied` from Foundry | Role not assigned or not propagated yet (up to 5 min); `az role assignment list --scope <foundry id>`. |
| Fleet: Azure `429` although the Fleet log shows `Injected LLM auth proxy header` | The proxy path worked; raise the model deployment's capacity. |
| LangSmith render: `Duplicate keys detected: [SSRF_ALLOW_K8S_INTERNAL]` | Fleet pods hard-code it; remove the copy from `commonEnv` or the Fleet `extraEnv`. |

## Teardown

```bash
helm uninstall llm-auth-proxy -n <namespace>
kubectl delete secret llm-auth-proxy-oauth -n <namespace>                   # path B
kubectl delete networkpolicy llm-auth-proxy-jwks -n <langsmith-namespace>   # if you created it
az identity delete -n <identity> -g <rg>                                    # path A (the federated credential goes with it)
```
Then drop `-f langsmith-values.yaml` and `config.signingJwks` (or the secret key) from your LangSmith upgrade and run it
again, untick the organization setting, and delete the model configuration and the placeholder secret in the UI.

## Not included

Covered by the [official page](https://docs.langchain.com/langsmith/llm-auth-proxy-self-hosted) and not repeated here: LangSmith SaaS, request and response body
transformation with `ext_proc` (not needed for Azure OpenAI's v1 API), ingress and autoscaling options, and the full
chart reference. Not covered anywhere in this kit: Terraform/Bicep for the Azure objects (the `az` commands above),
certificate-based client authentication for the app registration, TLS ingress for the proxy, per-workspace identities (design note
above), Envoy access logging, alerting.

## Tested with

LangSmith Helm chart 0.16.31 and 0.17.0-rc.36, `langsmith-auth-proxy` chart 0.0.15 (Envoy 1.37).

## Alternative without a proxy

LangSmith can obtain client-credentials tokens itself:
[OAuth client credentials on a model configuration](https://docs.langchain.com/langsmith/model-configurations#oauth-client-credentials)
takes a client id, secret, token URL and scope per configuration and needs no proxy. That is path B without a proxy, set
up by each workspace. The proxy earns its place when you want managed identity (no secret anywhere), one identity and role
assignment governed by the platform team, and a central log of who called the model. A configuration with OAuth enabled
bypasses the proxy, so both can coexist in one organization.

## Possible extensions

- **One identity per workspace.** The sidecar caches a single token for a single identity and only logs the claims. Using
  the `workspace_id` claim to choose the identity would mean one `Credential` (token, expiry, lock) per client id, a map
  from workspace to client id, a denial for unknown workspaces, and a readiness probe that mints for a default identity.
  On Azure the same pod token can be exchanged for any managed identity whose federated credential names the proxy's
  ServiceAccount, so only the client id in the exchange differs.
