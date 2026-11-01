# Deploying the public demo (single VM, automatic HTTPS)

Total cost: a small VM (~$5/month) plus OpenRouter usage, which is capped by the agent's daily budgets
(default ≈ 4M tokens/day on Claude Haiku 5.5 ≈ $0.40/day worst case).

## 1. Get a server and a domain

* Any Linux VM with 1 vCPU / 2 GB RAM works (Hetzner CX22, DigitalOcean Basic, Lightsail…).
* Point a DNS `A` record (e.g. `mcp-demo.yourname.dev`) at the VM's public IP.
* Open ports **80** and **443** in the VM's firewall. Nothing else needs to be public.

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

## 3. Deploy

```bash
git clone https://github.com/<you>/mini-mcp-gateway.git && cd mini-mcp-gateway
cp deploy/.env.example .env
nano .env        # set DOMAIN, OPENROUTER_API_KEY, PAYMENTS_API_TOKEN
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build --wait
```

Caddy obtains a Let's Encrypt certificate automatically on first request. Open `https://<DOMAIN>`.

**Tip:** create a dedicated OpenRouter key for the demo with a **credit limit** in the OpenRouter dashboard — a second,
independent cap on top of the gateway's own budgets.

## 4. Verify

```bash
curl -s https://<DOMAIN>/v1/meta          # public_demo: true, agent.available: true
curl -s https://<DOMAIN>/readyz           # 5 servers, 20 tools
pip install playwright && playwright install chromium
python console/e2e/ui_smoke.py --url https://<DOMAIN>
```

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs -f gateway` |
| Update | `git pull && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build --wait` |
| Stop | `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml down` |
| Wipe state | add `-v` to `down` (registry DB, audit log, certificates) |
| Tune limits/budgets | edit `deploy/demo.overlay.yaml`, then `docker compose … restart gateway` |

Agents can use the deployment too:

```bash
claude mcp add --transport http mcp-demo https://<DOMAIN>/mcp --header "Authorization: Bearer demo-rider-key"
```
