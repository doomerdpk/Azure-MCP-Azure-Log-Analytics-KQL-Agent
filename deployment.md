# Deployment Guide: Azure OpenAI KQL Agent (Streamlit + MCP) on App Service

**Architecture:** Streamlit UI → Azure OpenAI → local MCP module (imported directly, not a separate service) → Azure Log Analytics. Single App Service deployment.

**Security approach (learning-appropriate):** env vars in App Service Configuration instead of `.env`, Managed Identity for Log Analytics access, API key auth kept for Azure OpenAI (simpler, still secrets-safe via App Service Config).

---

## Prerequisites

- Azure CLI installed and logged in (`az login`)
- Project root contains `app.py`, `azure_mcp.py`, `requirements.txt`
- Local `.env` has: `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_LOG_ANALYTICS_WORKSPACE_ID`

---

## Step 1: Create Resource Group, Plan, Web App

```bash
az group create --name rg-ai-learning --location eastus

az appservice plan create \
  --name plan-kql-agent \
  --resource-group rg-ai-learning \
  --sku B1 \
  --is-linux \
  --location centralindia

az webapp create \
  --resource-group rg-ai-learning \
  --plan plan-kql-agent \
  --name kql-agent-azure-mcp \
  --runtime "PYTHON:3.11"
```

> B1 tier costs ~$13/month if left running. Delete the resource group or stop the app when not in use.

---

## Step 2: Set Environment Variables

```bash
az webapp config appsettings set \
  --resource-group rg-ai-learning \
  --name kql-agent-azure-mcp \
  --settings \
    AZURE_OPENAI_API_VERSION="<value>" \
    AZURE_OPENAI_ENDPOINT="<value>" \
    AZURE_OPENAI_API_KEY="<value>" \
    AZURE_LOG_ANALYTICS_WORKSPACE_ID="<value>"

az webapp config appsettings set \
  --resource-group rg-ai-learning \
  --name kql-agent-azure-mcp \
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

**Always verify** each `set` actually stuck (the CLI can silently no-op):

```bash
az webapp config appsettings list \
  --resource-group rg-ai-learning \
  --name kql-agent-azure-mcp \
  -o table
```

---

## Step 3: Enable Managed Identity + Grant Log Analytics Access

```bash
az webapp identity assign \
  --resource-group rg-ai-learning \
  --name kql-agent-azure-mcp
# copy the returned principalId

az monitor log-analytics workspace show \
  --resource-group rg-ai-learning \
  --workspace-name <your-workspace-name> \
  --query id -o tsv
# copy the returned workspace resource ID

az role assignment create \
  --assignee <principalId> \
  --role "Log Analytics Reader" \
  --scope <workspace-resource-id>
```

Your `DefaultAzureCredential()` call in `azure_mcp.py` will pick this up automatically once deployed — no code change needed.

_(Azure OpenAI auth kept as API key for this deployment — Managed Identity + `Cognitive Services OpenAI User` role is the natural next step if you want to remove the key entirely.)_

---

## Step 4: Set Startup Command

```bash
az webapp config set \
  --resource-group rg-ai-learning \
  --name kql-agent-azure-mcp \
  --startup-file "python -m streamlit run app.py --server.port 8000 --server.address 0.0.0.0"
```

---

## Step 5: Package and Deploy

```bash
# zip not available? use Python instead:
python3 -c "
import zipfile, os
exclude_dirs = {'venv', '.venv', '__pycache__', '.git'}
exclude_files = {'.env'}
with zipfile.ZipFile('deploy.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for file in files:
            if file in exclude_files or file.endswith('.pyc'):
                continue
            filepath = os.path.join(root, file)
            zf.write(filepath, os.path.relpath(filepath, '.'))
print('deploy.zip created')
"

az webapp restart --resource-group rg-ai-learning --name kql-agent-azure-mcp

az webapp deploy \
  --resource-group rg-ai-learning \
  --name kql-agent-azure-mcp \
  --src-path deploy.zip \
  --type zip
```

---

## Troubleshooting Notes (from this deployment)

**Symptom:** Container exits with code 1 during startup; site stuck "Starting..."; orchestration logs (`az webapp log tail`, `log download`) show no Python traceback, only container lifecycle messages.

**Root cause found here:** `SCM_DO_BUILD_DURING_DEPLOYMENT=true` didn't actually get applied when first set (silent no-op) → Kudu never ran `pip install -r requirements.txt` → no `antenv` virtual environment created → all third-party imports (`azure.identity`, etc.) failed with `ModuleNotFoundError`.

**How it was diagnosed:**

1. `az webapp log tail` / `log download` — only showed container manager status, not app errors.
2. App container can't be reached via `az webapp ssh` while crash-looping — use the **Kudu SCM Bash console** instead: `https://<app-name>.scm.azurewebsites.net/newui/` → Debug console → Bash. This container is always-on regardless of app health.
3. In Kudu console: `cd /home/site/wwwroot && python3 -c "from azure_mcp import ..."` reproduced the exact import error directly.
4. Checked for `antenv/` (Oryx's build venv) — didn't exist. Checked deployment log for `pip`/`oryx` activity — none found. Checked `env | grep SCM_DO_BUILD` — empty. Confirmed via `az webapp config appsettings list` outside the container that the setting was genuinely missing.

**Fix:** Re-run `appsettings set` for `SCM_DO_BUILD_DURING_DEPLOYMENT=true`, **immediately verify with `appsettings list`** (don't trust the `set` output alone — it can show `null` even when it worked, or silently fail), restart the app, then redeploy.

**Key lesson:** Always verify App Service Configuration changes with a separate `list`/`show` call. Don't assume a `set` command succeeded just because it returned without an error.

---

## Verification Checklist

- [ ] `appsettings list` shows all 4 secrets + `SCM_DO_BUILD_DURING_DEPLOYMENT=true`
- [ ] Managed Identity enabled (`az webapp identity show`)
- [ ] Role assignment exists for `Log Analytics Reader` on the workspace
- [ ] Deploy output shows Oryx/pip install activity (not just "Build successful" with 0s duration)
- [ ] App loads at `https://kql-agent-azure-mcp.azurewebsites.net`
- [ ] A test question in the UI returns KQL + results without auth errors
