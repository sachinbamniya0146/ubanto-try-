# Oracle Free Tier Automation - GitHub Actions

This repository automates the creation of Oracle Cloud Free Tier instances (ARM A1.Flex or E2.1.Micro) using GitHub Actions. It runs every 5 minutes to check for available capacity and create instances.

## Features

- ✅ **Automated retry every 5 minutes** via GitHub Actions schedule
- ✅ **Private repository** - only you have access
- ✅ **Secure secrets management** - all credentials stored in GitHub Secrets
- ✅ **Discord/Email notifications** on success/failure
- ✅ **Logs as artifacts** - 30 day retention
- ✅ **Manual trigger** - run anytime from GitHub Actions UI
- ✅ **Network error handling** - automatic retries with exponential backoff
- ✅ **Rate limit handling** - 429 errors wait 2+ minutes

## Quick Setup

### 1. Create the Repository
```bash
# This repo is already created locally at ~/oracle-freetier-automation
# Push to GitHub (replace YOUR_USERNAME):
cd ~/oracle-freetier-automation
git init
git add .
git commit -m "Initial commit: Oracle Free Tier automation"
gh repo create oracle-freetier-automation --private --source=. --push
```

### 2. Add Required Secrets

Go to your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | Value | How to Get |
|-------------|-------|------------|
| `OCI_CONFIG_B64` | Base64 of `~/.oci/config` | `base64 -i ~/.oci/config` |
| `OCI_KEY_B64` | Base64 of OCI private key | `base64 -i ~/oci-repeater/resources/oci-keyfile.pem` |
| `SSH_PUB_B64` | Base64 of SSH public key | `base64 -i ~/.oci/sachin_vps.pub` |
| `OCI_ENV_B64` | Base64 of oci.env file | `base64 -i ~/oci-repeater/freetier/oci.env` |
| `EMAIL_PASSWORD` | Gmail App Password | Generate at myaccount.google.com/apppasswords |
| `DISCORD_WEBHOOK` | Discord webhook URL | Create in Discord Server Settings → Integrations |

### 3. Enable Workflow
After pushing, go to **Actions** tab and enable workflows if prompted.

### 4. Test Run
Go to **Actions → Oracle Free Tier Instance Creation → Run workflow** to test manually.

## Required Files (already in repo)

- `main.py` - Core Oracle instance creation logic
- `email_content.html` - Email template for notifications
- `requirements.txt` - Python dependencies
- `.github/workflows/oracle-freetier.yml` - GitHub Actions workflow

## Configuration (via oci.env secrets)

The `OCI_ENV_B64` secret contains your configuration:
```env
OCI_CONFIG=/home/runner/.oci/config
OCT_FREE_AD=AD-1
DISPLAY_NAME=sachin-ubuntu24-a1
REQUEST_WAIT_TIME_SECS=60
SSH_AUTHORIZED_KEYS_FILE=/home/runner/.ssh/oracle_key.pub
OCI_IMAGE_ID=
OCI_COMPUTE_SHAPE=VM.Standard.A1.Flex
SECOND_MICRO_INSTANCE=false
OCI_SUBNET_ID=ocid1.subnet.oc1.ap-hyderabad-1.aaaaaaaaoar36v2vxfqlhvselrqqeb4nq47klbxas4srw2cmqzekb74y7u6a
OPERATING_SYSTEM=Canonical Ubuntu
OS_VERSION=24.04
ASSIGN_PUBLIC_IP=true
BOOT_VOLUME_SIZE=100
NOTIFY_EMAIL=false
EMAIL=
DISCORD_WEBHOOK=
```

## How It Works

1. **Schedule**: Runs every 5 minutes (`*/5 * * * *`)
2. **Setup**: Decodes secrets → configures OCI CLI, SSH keys, oci.env
3. **Execute**: Runs `python main.py --once` (single attempt)
4. **Check**: If `INSTANCE_CREATED` file exists → success notification
5. **Artifacts**: Uploads all logs for debugging
6. **Next run**: Automatic retry in 5 minutes

## Local Development

```bash
# Test locally (uses your local ~/.oci/config)
cd ~/oracle-freetier-automation
python main.py --once

# Run continuous loop (original behavior)
python main.py
```

## Monitoring

- **GitHub Actions**: View runs at `https://github.com/YOUR_USERNAME/oracle-freetier-automation/actions`
- **Logs**: Download artifacts from each run
- **Discord**: Real-time notifications on create/fail
- **Email**: HTML formatted instance details

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Permission denied" on key | Ensure `OCI_KEY_B64` is correct private key |
| "Invalid compartment" | Check `OCI_CONFIG_B64` has correct tenancy OCID |
| "LimitExceeded" | Normal - script retries automatically |
| No runs showing | Enable workflows in Actions tab |

## Security Notes

- ✅ Private repo - only you can see runs/secrets
- ✅ No credentials in code - all in GitHub Secrets
- ✅ OCI key restricted to your tenancy
- ✅ SSH key only used for instance access
- ✅ Logs auto-expire after 30 days

---

**Generated for your Oracle Free Tier automation. Happy cloud hunting! 🚀**