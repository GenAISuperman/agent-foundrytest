# agent-template-foundry

Template for agents that run on the **Foundry** runtime as a hosted agent
(Microsoft Agent Framework, own container). The platform portal creates agent
repositories from this template; developers do not clone it directly.

This repository must be marked as a GitHub template (Settings, General,
"Template repository", or `gh api -X PATCH repos/<org>/agent-template-foundry -f is_template=true`).
Generated repositories are named `agent-<id>`.

## How it works

- `src/app.py` hosts the agent with Foundry's Invocations protocol
  (`azure-ai-agentserver-invocations`): `POST /invocations`, `GET /readiness`, port 8088.
- `src/chat_completions.py` is the only place that knows the request and response
  body shape: an OpenAI-style chat-completions body in and out, the same contract as AKS agents.
- `src/config.py` reads and validates `agent.yaml` at startup. An invalid file stops the
  container with a message that starts `Invalid agent.yaml`.
- `prompts/system.txt` is the system prompt. It is read on every request, so edits
  change behaviour with no code change (rebuild the image to ship an edit).
- `src/tools.py` turns the tools bound in `agent.yaml` into function tools (see Tools below).
- `src/tokens.py` is the only place that fetches a token for a tool call.
- `opa/agent-policy.rego` is the CI policy for `agent.yaml` (see OPA policy below).
- The model is reached with `DefaultAzureCredential`. No model key is used or stored.

## Placeholders

Replaced across the repository by provisioning, using the same `{{NAME}}` mechanism as the AKS template.

| Placeholder | Used in |
|-------------|---------|
| `{{AGENT_ID}}` | `agent.yaml` (`agent.id`, `foundry.agent_name`) |
| `{{TEAM_NAME}}` | `agent.yaml` |
| `{{INTERACTION_MODEL}}` | `agent.yaml` |
| `{{GUARDRAIL_PROFILE}}` | `agent.yaml` |
| `{{FOUNDRY_PROJECT_ENDPOINT}}` | `agent.yaml` |
| `{{MODEL_DEPLOYMENT}}` | `agent.yaml` |
| `{{VIOLATION_THRESHOLD}}`, `{{WINDOW_MINUTES}}`, `{{OUTPUT_ACTION}}`, `{{NOTIFY_WEBHOOK}}` | `agent.yaml` (`output_policy`) |

The agent's identity id is not stored in `agent.yaml`.

## Run locally

From a generated repository (the template itself refuses to start while placeholders remain):

```bash
docker build --platform linux/amd64 -t my-agent .
docker run --rm -p 8088:8088 \
  -e FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project> \
  -e AZURE_AI_MODEL_DEPLOYMENT_NAME=<deployment> \
  -e AZURE_TENANT_ID=... -e AZURE_CLIENT_ID=... -e AZURE_CLIENT_SECRET=... \
  my-agent
```

The two `FOUNDRY_*`/`AZURE_AI_*` variables override the `foundry` block in `agent.yaml`
(the platform injects the same names when hosted). The `AZURE_*` credential variables are for local runs only and
feed `DefaultAzureCredential`; without Docker, run `az login` then `python -m src.main`.

```bash
curl -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"model": "my-agent", "messages": [{"role": "user", "content": "Hello"}]}'
```

Response:

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1790000000,
  "model": "<agent-id>",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
}
```

A malformed body gets HTTP 400 with `{"error": {"message": ..., "type": "invalid_request_error"}}`.
Streaming (`"stream": true`) is not supported.

## Tools

Tools are bound to the agent in the `tools` list of `agent.yaml` (the portal's bind pull request
writes the entries; `tools: []` is valid and the agent just answers without tools):

```yaml
tools:
  - id: mock-shop-api
    roles: [read]
    apim_base_url: https://<apim>.azure-api.net/tools/mock-shop-api
    scope: api://<tool-app-id>/.default
```

All four fields are required, and `id` must be unique in the file (a duplicate stops startup). `apim_base_url`
must start with `https://`: the agent sends its own bearer token there. Plain `http://` is accepted only for
`localhost`, `127.0.0.1` and `host.docker.internal`, for local runs against the mock tool server. Startup validation
and the OPA policy enforce both rules.

At startup the agent fetches `<apim_base_url>/openapi.json`
for each bound tool and offers the model one function per operation in it, named
`<tool-id>_<operationId>` (for example `mock-shop-api_get-products`). A call fetches the agent's own
token for `scope` and calls the route on APIM, so APIM's per-operation role check decides.

How an operation becomes a function:

- Parameters come from the path item and from the operation (the operation wins for the same name and location),
  and `#/components/parameters/...` references are resolved. Remote references are never followed.
- Path and query parameters become arguments; their declared type (string, integer, number, boolean, array of those)
  and enum are in the function schema. Booleans go on the wire as `true`/`false`, arrays as repeated query keys.
- A path placeholder the export did not declare is still a required string argument. A path argument that is empty,
  `.` or `..` is refused (nothing is sent), and every path value is URL-encoded.
- A POST, PUT or PATCH (or any operation with a `requestBody`) takes a `body` object. A parameter that is itself
  called `body` is offered as `query_body` (or `path_body`) instead, and the same applies to a name used in two locations.
- A required header or cookie parameter cannot be sent (`Accept`, `Content-Type` and `Authorization` aside). The
  function says so in its description and a call returns that reason to the model without calling the tool.
- If function names collide (duplicate operation ids, characters replaced by `_`, truncation to 64 characters), every
  colliding function gets a short hash suffix, so names do not depend on the order of the export.

- The agent only calls tools listed in its deployed `agent.yaml`. URLs always come from `apim_base_url`,
  never from the OpenAPI document or the model, and path arguments are URL-encoded.
- Every operation in the export is offered, whatever `roles` says; a refused call (HTTP 401 or 403) comes back to the
  model as an explanation, so it tells the user it has no access and the request still completes.
- A tool whose export cannot be fetched (APIM down, token refused) is logged and the agent still starts. Startup waits
  at most 15 seconds for all exports together. A failed tool is retried by a background task, never inside a request,
  after a cooldown of 5 seconds that doubles per failure up to 5 minutes; requests meanwhile use the tools that loaded.
- The token comes from `DefaultAzureCredential().get_token(scope)` in `src/tokens.py`. Whether a hosted
  agent's own token carries the tool's roles is not yet verified on a real Foundry resource; if it does not,
  that one function is what changes.

### Run locally with a mock tool

`dev/mock_tool_server.py` stands in for APIM (standard library only). It serves the OpenAPI export and the
routes of `mock-shop-api` (read: list and get products; write: add to basket) and `other-api`.

```bash
python -m dev.mock_tool_server --port 8001
```

Copy your `agent.yaml` to `agent.local.yaml`, set the tool entry's `apim_base_url` to
`http://host.docker.internal:8001/tools/<id>`, then run the same container with the mock token source and that file:

```bash
docker run --rm -p 8088:8088 \
  -v "$PWD/agent.local.yaml:/app/agent.local.yaml:ro" -e AGENT_YAML_PATH=/app/agent.local.yaml \
  -e AGENT_LOCAL_MOCK_TOOLS=1 \
  -e FOUNDRY_PROJECT_ENDPOINT=... -e AZURE_AI_MODEL_DEPLOYMENT_NAME=... \
  -e AZURE_TENANT_ID=... -e AZURE_CLIENT_ID=... -e AZURE_CLIENT_SECRET=... \
  my-agent
```

`AGENT_LOCAL_MOCK_TOOLS=1` makes the agent send a fake bearer token (`mock-token`) instead of asking Azure for one.
The mock accepts any token and does not enforce roles, so it cannot show a 403; never set the variable on a hosted agent
(real APIM would reject the fake token and every tool call would fail).

## OPA policy

CI validates `agent.yaml` against `opa/agent-policy.rego`. The policy has a Foundry branch: it requires the
`foundry` block, denies a non-Foundry runtime or an AKS `identity` block, denies any `{{PLACEHOLDER}}` left in the
file, and checks each tool entry has `id` (unique), `roles` (non-empty), `apim_base_url` (https, or http for local
hosts only) and `scope`.

```bash
opa test opa/ -v
opa eval --format raw --input agent.yaml --data opa/agent-policy.rego 'data.agent.policy.deny'
```

An empty `deny` set means the file is valid. (An unquoted `{{...}}` placeholder is not parseable YAML, so a leftover
one in `output_policy` fails at parse time instead; the policy catches quoted ones.)

## Tests

The tests start the real web layer as a server process with a stub model and call it over HTTP. Tool parsing is also
tested in process against `tests/openapi_stub.py`, which serves any OpenAPI document. The tests render
`tests/fixtures/agent.template.yaml`, a copy of the template's `agent.yaml` (so they pass in generated repositories, whose
`agent.yaml` is filled in); in the template a test fails if the copy and `agent.yaml` differ, so edit both together.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## CI/CD

Workflows live in `.github/workflows/`. Except for the `tests` job, they do not run in the template repository itself
(the jobs skip when the repository is a GitHub template).

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `ci.yml` | pull request to `main` | **Policy and unit tests** (also runs in the template repository): `opa test opa/ -v` and `pytest`, with OPA pinned to a release and checked against its published sha256 (`OPA_VERSION`, `OPA_SHA256` at the top of the file). **Policy check**: `opa eval` of `agent.yaml` against `opa/` (`data.agent.policy.deny` must be empty). **Image build**: `docker build --platform linux/amd64`. |
| `deploy.yml` | push to `main`, or manual **Run workflow** | Logs in to Azure with GitHub OIDC (`azure/login`, no stored secret), builds and pushes the image, creates a new hosted-agent version, reads the agent identity id and reports it to the portal. |

No evaluation runs in either workflow, and no model key is used anywhere.

### Repository secrets (set by provisioning, ticket 09)

| Secret | Value |
|--------|-------|
| `AZURE_CLIENT_ID` | Client id of the repository's deployer identity (federated to this repository's `main` branch) |
| `AZURE_TENANT_ID` | Entra tenant id |
| `AZURE_SUBSCRIPTION_ID` | Subscription id |
| `ACR_LOGIN_SERVER` | Registry server, for example `acragenticdemopoc.azurecr.io` |
| `FOUNDRY_PROJECT_ENDPOINT` | `https://<resource>.services.ai.azure.com/api/projects/<project>` |
| `PORTAL_URL` | Portal base URL with no path, for example `https://portal.example.com`. The last deploy step posts to `<PORTAL_URL>/api/agents/<agent id>/identity` |
| `TEAM_NAME` | Owning team |
| `APIM_BASE_URL` | Gateway base URL |

`TEAM_NAME` and `APIM_BASE_URL` are provisioned for parity with the AKS template (their values are
also written into `agent.yaml`); the current workflows do not read them. The deployer identity needs
*Foundry Project Manager* on the Foundry project and *AcrPush* on the registry, and the registry
must let the Foundry project's managed identity pull (see the platform infrastructure).

### Deploy steps

1. `az acr login`, then build and push `<ACR_LOGIN_SERVER>/agents/<agent id>:<sha12>-<run id>-<attempt>`.
   Each deploy gets its own immutable tag, so older versions keep pulling the image they were created from.
2. `python -m deploy.create_agent_version` creates a `hosted` agent version with the stable
   `azure-ai-projects` SDK (`agents.create_version`, container image, `invocations` protocol, 1 CPU, 2Gi)
   and waits for it to reach `active`. The endpoint follows the latest version.
3. `python -m deploy.agent_identity` prints the agent identity id.
4. `python -m deploy.report_identity` posts `{"agentIdentityId": "<id>"}` to the portal. The route is
   unauthenticated by accepted POC decision. Redirects are not followed and count as failures (a redirected POST
   would otherwise turn into a GET that looks successful); timeouts, dropped connections, HTTP 408/425/429 and 5xx are
   retried three times, any other 4xx fails at once.

**Re-runs.** *Re-run all jobs* or *Run workflow* (`workflow_dispatch`) on `main` creates a new version
without a commit: every run passes a new `AGENT_DEPLOY_ID` environment variable, so the definition differs
and Foundry records a new version.

**Failed deploys.** Nothing is ever deleted. If the image build or push, the version creation or its
provisioning fails, the job stops with a non-zero exit before the identity is reported, and the
versions that already exist stay in place. If only the portal callback fails (the version is already
live), re-run the workflow.

### Assumptions to verify (spike ticket 02 has not run)

- **Reading the identity id** is isolated in `deploy/agent_identity.py`. It reads
  `instance_identity.principal_id` from the new agent version, falling back to the same field on the
  agent. `principal_id` is the identity's service principal object id, which is what the portal needs for
  Graph app-role assignment. If the spike shows a different field or call, change only that file.
- The `invocations` protocol is declared with version `2.0.0`, as in Microsoft Learn's hosted-agent examples
  (`PROTOCOL_VERSION` in `deploy/create_agent_version.py`).
- That a failed new version does not take over the endpoint's "latest" selector is Foundry behaviour not yet
  confirmed against a real project.

### Deploy script tests

```bash
pytest tests/test_deploy_create_version.py tests/test_deploy_identity.py
```

They run the scripts against a faked Foundry client and a local stand-in for the portal.
