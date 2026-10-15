# Deploy ai-agent-platform to AWS EKS

The cluster, ECR repositories, the `ai-agent-platform` namespace and the deploy role come from the **aws-infra** repository.
The agent and the sample app both run in the `ai-agent-platform` namespace; the agent may change only the `sample-app` deployment.

## GitHub settings (once)

Variables: `AWS_REGION`, `EKS_CLUSTER_NAME`, `ECR_REGISTRY`, `AWS_DEPLOY_ROLE_ARN` (from `terraform output app_repo_github_variables` in aws-infra).

Secrets: `AGENT_API_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` (optional).

## Deploy / remove

GitHub → **Actions** → **CD** → **Run workflow** → `deploy` or `uninstall`.

## Use it

```bash
aws eks update-kubeconfig --name karthik-eks --region ap-south-1
kubectl port-forward -n ai-agent-platform svc/sample-app 8081:80
kubectl port-forward -n ai-agent-platform svc/ai-agent 8090:80
curl -X POST http://127.0.0.1:8081/simulate-failure
curl -s -H "Authorization: Bearer <AGENT_API_TOKEN>" http://127.0.0.1:8090/incidents
```

## Local (kind) with the same chart

Remove the old raw-manifest install first: `kubectl delete namespace ai-agent sample-app --ignore-not-found`.

```bash
kubectl create namespace ai-agent-platform
kubectl create secret generic ai-agent-secrets -n ai-agent-platform --from-env-file=.env
helm upgrade --install ai-agent-platform helm/ai-agent-platform -n ai-agent-platform -f helm/ai-agent-platform/values-local.yaml
```

> Full step-by-step EKS runbook (bring up, deploy, test, tear down): **aws-infra/README.md**
