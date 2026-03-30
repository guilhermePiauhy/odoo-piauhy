# odoo-piauhy

Odoo 17 Docker infrastructure with separate production and development stacks.

- **Production:** Odoo 17 + PostgreSQL 16 + Nginx (reverse proxy, HTTPS-ready)
- **Development:** Odoo 17 + PostgreSQL 16, direct port access, debug mode enabled

---

## Repository Layout

```
odoo-piauhy/
├── docker-compose.prod.yml   # Production stack
├── docker-compose.dev.yml    # Development stack
├── .env.example              # Environment variable template
├── config/
│   └── odoo.conf             # Shared Odoo configuration
├── nginx/
│   └── odoo.conf             # Nginx reverse proxy config
└── addons/                   # Custom Odoo modules (mounted into both stacks)
```

---

## Prerequisites

- Docker >= 24.x
- Docker Compose plugin >= 2.x
- (Production) A domain with DNS pointing to the server
- (Production) TLS certificates (Let's Encrypt recommended)

---

## First-Time Setup

### 1. Clone the repository

```bash
git clone https://github.com/guilhermePiauhy/odoo-piauhy.git
cd odoo-piauhy
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in every `CHANGE_ME_*` value:

| Variable | Description |
|---|---|
| `POSTGRES_DB` | Database name (default: `odoo`) |
| `POSTGRES_USER` | DB user (default: `odoo`) |
| `POSTGRES_PASSWORD` | Strong random password for PostgreSQL |
| `ODOO_ADMIN_PASSWD` | Master password for `/web/database` manager |
| `DOMAIN` | Your public domain (production only) |
| `TLS_CERT` / `TLS_KEY` | Paths to your TLS certificate files (production only) |

Generate strong passwords:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Set the admin password in `config/odoo.conf`

Replace `CHANGE_ME_USE_ENV_VAR` with the same value you placed in `ODOO_ADMIN_PASSWD`:

```ini
admin_passwd = <your-strong-admin-password>
```

---

## Development Environment

### Start

```bash
docker compose -f docker-compose.dev.yml --env-file .env up -d
```

### Access

| Service | URL |
|---|---|
| Odoo web | http://localhost:8069 |
| PostgreSQL | localhost:5432 |

### Logs

```bash
# All services
docker compose -f docker-compose.dev.yml logs -f

# Odoo only
docker compose -f docker-compose.dev.yml logs -f odoo
```

### Stop

```bash
docker compose -f docker-compose.dev.yml down
```

### Stop and remove volumes (full reset)

```bash
docker compose -f docker-compose.dev.yml down -v
```

### Scaffold a new custom module

```bash
docker exec odoo_dev_app odoo scaffold my_module /mnt/extra-addons
```

The module will appear immediately in `./addons/my_module/`.

---

## Production Environment

### TLS Certificates

Place your certificate files at the paths defined in `.env`:

```
nginx/certs/fullchain.pem
nginx/certs/privkey.pem
```

**Let's Encrypt (Certbot) — recommended approach:**

```bash
# Install Certbot on the host
sudo apt install certbot

# Issue a standalone certificate (stop Nginx first if already running)
sudo certbot certonly --standalone -d odoo.example.com

# Update .env
TLS_CERT=/etc/letsencrypt/live/odoo.example.com/fullchain.pem
TLS_KEY=/etc/letsencrypt/live/odoo.example.com/privkey.pem
```

Then update the volume mounts in `docker-compose.prod.yml` under the `nginx` service to point to the Certbot paths.

### Update the domain in Nginx config

Open `nginx/odoo.conf` and replace `server_name _;` with your actual domain in both server blocks:

```nginx
server_name odoo.example.com;
```

### Start

```bash
docker compose -f docker-compose.prod.yml --env-file .env up -d
```

### Verify health

```bash
# Check all services are running
docker compose -f docker-compose.prod.yml ps

# PostgreSQL health
docker inspect --format='{{.State.Health.Status}}' odoo_prod_db

# Odoo logs
docker compose -f docker-compose.prod.yml logs -f odoo
```

### Access

| Route | Description |
|---|---|
| `http://<domain>` | Redirects to HTTPS |
| `https://<domain>` | Odoo web interface |
| `https://<domain>/web/database/manager` | Blocked by Nginx (403) |

### Stop

```bash
docker compose -f docker-compose.prod.yml down
```

---

## Custom Addons

Place your custom modules in the `./addons/` directory. The directory is mounted at `/mnt/extra-addons` inside the Odoo container for both stacks.

After adding a module, activate developer mode in Odoo and run:

**Settings > Technical > Update Apps List**

Or trigger it via CLI:

```bash
# Development
docker exec odoo_dev_app odoo -u my_module --stop-after-init

# Production (with database name)
docker exec odoo_prod_app odoo -d odoo -u my_module --stop-after-init
```

---

## Volumes

| Volume | Description |
|---|---|
| `odoo_prod_postgres_data` | Production PostgreSQL data |
| `odoo_prod_odoo_data` | Production Odoo filestore and sessions |
| `odoo_dev_postgres_data` | Development PostgreSQL data |
| `odoo_dev_odoo_data` | Development Odoo filestore and sessions |

### Backup PostgreSQL (production)

```bash
docker exec odoo_prod_db pg_dump -U odoo odoo | gzip > odoo_backup_$(date +%Y%m%d_%H%M%S).sql.gz
```

### Restore PostgreSQL

```bash
gunzip -c odoo_backup_20260101_120000.sql.gz | docker exec -i odoo_prod_db psql -U odoo odoo
```

---

## Tuning Production Workers

Edit `config/odoo.conf` and adjust based on your server's CPU cores:

```ini
workers = 4            # Rule of thumb: (CPU cores * 2) + 1
max_cron_threads = 2
```

Restart Odoo after changes:

```bash
docker compose -f docker-compose.prod.yml restart odoo
```

---

## Security Checklist

- [ ] `POSTGRES_PASSWORD` set to a strong random value (not the example)
- [ ] `ODOO_ADMIN_PASSWD` set to a strong random value (not the example)
- [ ] `admin_passwd` in `config/odoo.conf` matches `ODOO_ADMIN_PASSWD`
- [ ] `.env` is listed in `.gitignore` and never committed
- [ ] TLS certificate is valid and auto-renewing
- [ ] `nginx/odoo.conf` `server_name` set to your real domain
- [ ] `/web/database/` route blocked by Nginx (already configured)
- [ ] `list_db = False` set in `config/odoo.conf` once your DB is created (prevents DB list exposure)
- [ ] HSTS header uncommented in `nginx/odoo.conf` after confirming HTTPS works

---

## Troubleshooting

**Odoo cannot connect to the database**

Check that the `db` service is healthy before Odoo starts:

```bash
docker compose -f docker-compose.prod.yml ps db
# Status should show: healthy
```

If it shows `starting`, wait a few seconds and check again. The `depends_on: condition: service_healthy` clause handles this automatically on start.

**502 Bad Gateway from Nginx**

The Odoo container may still be initializing. Check its logs:

```bash
docker compose -f docker-compose.prod.yml logs odoo
```

**Port 5432 already in use (development)**

Another PostgreSQL instance is running on the host. Either stop the host process or change the host port mapping in `docker-compose.dev.yml`:

```yaml
ports:
  - "5433:5432"   # Map to 5433 on the host instead
```
