# Deploy ai-agent-platform to AWS EKS

> The complete, ordered runbook for all three repositories (bootstrap, cluster, deploy, test, tear down) is in **aws-infra/README.md**. This page covers only this repository.

The cluster, ECR repositories, the `ai-agent-platform` namespace and the deploy role come from the **aws-infra** repository - create them first (aws-infra → **Bootstrap** `apply`, then **Terraform** `apply`).

## 1. GitHub settings (once)

**UI:** this repo → **Settings** → **Secrets and variables** → **Actions**.

| Type | Name | Value |
|---|---|---|
| Variable | `AWS_REGION` | `ap-south-1` |
| Variable | `EKS_CLUSTER_NAME` | `karthik-eks` |
| Variable | `ECR_REGISTRY` | `955151282954.dkr.ecr.ap-south-1.amazonaws.com` |
| Variable | `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::955151282954:role/karthik-platform-github-deploy` |
| Secret | `AGENT_API_TOKEN` | from `.env` |
| Secret | `OPENAI_API_KEY` | from `.env` |
| Secret | `ANTHROPIC_API_KEY` | from `.env` (optional) |

## 2. Deploy

1. **Actions** → **CD** (left list) → **Run workflow**.
2. **Use workflow from:** `main` · **action:** `deploy` → **Run workflow**.
3. Open the run; wait for a green ✓. The **Smoke test** step prints `{"status":"healthy"}`.

What it does: tests → build `sample-app` and `ai-agent` → push to ECR (tag = commit SHA) → create the secret → Helm install into namespace `ai-agent-platform` → health check.

## 3. Connect

```bash
export AWS_PROFILE=admin
aws eks update-kubeconfig --name karthik-eks --region ap-south-1
kubectl get pods -n ai-agent-platform
```

✅ `ai-agent` and `sample-app` `1/1 Running`.

Window 1: `kubectl port-forward -n ai-agent-platform svc/sample-app 8081:80`
Window 2: `kubectl port-forward -n ai-agent-platform svc/ai-agent 8090:80`

> Every restart or rollback replaces the `sample-app` pod and breaks window 1's port-forward - restart it before the next test.

Window 3:

```bash
cd "/d/AI Projects/ai-agent-platform"
TOKEN=$(grep -m1 '^AGENT_API_TOKEN=' .env | cut -d= -f2-)
AUTH="Authorization: Bearer $TOKEN"
approve_latest() {
  ID=$(curl -s -H "$AUTH" http://127.0.0.1:8090/incidents | python -c "import sys,json; d=json.load(sys.stdin); p=[i for i in d if i.get('status')=='pending_approval']; print(p[-1]['id'] if p else '')")
  if [ -z "$ID" ]; then echo "No pending incident"; return; fi
  echo "Approving incident $ID"
  curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
    -d '{"approve": true}' http://127.0.0.1:8090/incidents/$ID/decision | python -m json.tool
}
curl -s http://127.0.0.1:8081/status; echo
curl -s http://127.0.0.1:8090/health; echo
curl -s -o /dev/null -w "No token: %{http_code}\n" http://127.0.0.1:8090/incidents
```

✅ `"healthy":true` · `{"status":"healthy"}` · `No token: 401`.

## 4. Test scenarios

### A. Unhealthy app → restart (watcher)

```bash
curl -s -X POST http://127.0.0.1:8081/simulate-failure; echo
sleep 50
curl -s -H "$AUTH" http://127.0.0.1:8090/incidents | python -m json.tool
approve_latest
kubectl get pods -n ai-agent-platform
```

✅ `"source": "watcher"`, `restart_deployment`; after approval `"executed": true`, `"healthy": true` and a new `sample-app` pod.

### B. Memory leak → investigate

Restart window 1, then:

```bash
curl -s -m 5 -X POST http://127.0.0.1:8081/simulate-memory-leak; echo
kubectl get pods -n ai-agent-platform -w
```

When `sample-app` shows `RESTARTS 1`, press Ctrl+C:

```bash
kubectl get pod -n ai-agent-platform -l app=sample-app -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}'; echo
curl -s -X POST -H "$AUTH" http://127.0.0.1:8090/incidents | python -m json.tool
```

✅ `OOMKilled`, then `"recommended_action": "investigate"`, reason `2`, no approval requested.

### C. Bad release → rollback

```bash
kubectl set env deployment/sample-app -n ai-agent-platform CRASH_ON_STARTUP=true
sleep 60
curl -s -H "$AUTH" http://127.0.0.1:8090/incidents | python -m json.tool
approve_latest
kubectl rollout status deployment/sample-app -n ai-agent-platform
kubectl get deployment sample-app -n ai-agent-platform -o jsonpath='{.spec.template.spec.containers[0].env}'; echo
```

✅ `"action": "rollback"`, reason `3`; after approval `from_revision N` → `to_revision N-1` and only `ENVIRONMENT` in the env list.

### D. Least privilege and audit log

```bash
SA=$(kubectl get deploy ai-agent -n ai-agent-platform -o jsonpath='{.spec.template.spec.serviceAccountName}')
kubectl auth can-i patch deployment/sample-app -n ai-agent-platform --as=system:serviceaccount:ai-agent-platform:$SA
kubectl auth can-i patch deployment/ai-agent   -n ai-agent-platform --as=system:serviceaccount:ai-agent-platform:$SA
kubectl auth can-i get secrets                  -n ai-agent-platform --as=system:serviceaccount:ai-agent-platform:$SA
kubectl auth can-i delete pods                  -n ai-agent-platform --as=system:serviceaccount:ai-agent-platform:$SA
MSYS_NO_PATHCONV=1 kubectl exec -n ai-agent-platform deploy/ai-agent -- tail -n 5 /app/audit/audit.jsonl
```

✅ `yes`, `no`, `no`, `no`, and one audit line per decision.

## 5. Remove

**Actions** → **CD** → **Run workflow** → action **`uninstall`**.

Before destroying the cluster, uninstall both apps (this repo and ai-rag-chatbot), then aws-infra → **Terraform** `destroy`, then **Bootstrap** `destroy`.
