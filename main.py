import configparser
import itertools
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import oci
import paramiko
from dotenv import load_dotenv
import requests

load_dotenv('oci.env')

ARM_SHAPE = "VM.Standard.A1.Flex"
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"

OCI_CONFIG = os.path.expanduser(os.getenv("OCI_CONFIG", "").strip())
OCT_FREE_AD = os.getenv("OCT_FREE_AD", "").strip()
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "arm-12g-server").strip()
WAIT_TIME = int(os.getenv("REQUEST_WAIT_TIME_SECS", "50").strip())
SSH_AUTHORIZED_KEYS_FILE = os.path.expanduser(os.getenv("SSH_AUTHORIZED_KEYS_FILE", "").strip())
OCI_IMAGE_ID = os.getenv("OCI_IMAGE_ID", None).strip() if os.getenv("OCI_IMAGE_ID") else None
OCI_COMPUTE_SHAPE = os.getenv("OCI_COMPUTE_SHAPE", ARM_SHAPE).strip()
SECOND_MICRO_INSTANCE = os.getenv("SECOND_MICRO_INSTANCE", 'False').strip().lower() == 'true'
OCI_SUBNET_ID = os.getenv("OCI_SUBNET_ID", None).strip() if os.getenv("OCI_SUBNET_ID") else None
OPERATING_SYSTEM = os.getenv("OPERATING_SYSTEM", "Canonical Ubuntu").strip()
OS_VERSION = os.getenv("OS_VERSION", "22.04").strip()
ASSIGN_PUBLIC_IP = os.getenv("ASSIGN_PUBLIC_IP", "true").strip()
BOOT_VOLUME_SIZE = os.getenv("BOOT_VOLUME_SIZE", "100").strip()
OCI_OCPUS = float(os.getenv("OCI_OCPUS", "2").strip())
OCI_MEMORY_GB = float(os.getenv("OCI_MEMORY_GB", "12").strip())

# Telegram — ONLY bot token needed. Chat ID auto-discovered.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Cache file for chat IDs (persists within one GitHub run via artifacts if needed)
_TG_CHAT_CACHE = Path("tg_chat_ids.txt")
_TG_CHAT_IDS = []


# ─────────────────────── TELEGRAM AUTO-DISCOVERY ───────────────────────
def tg_discover_chat_ids():
    """Call getUpdates to find all chat IDs that have messaged the bot."""
    global _TG_CHAT_IDS
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        r = requests.get(url, timeout=10)
        data = r.json()
        if not data.get("ok"):
            print(f"[Telegram] getUpdates failed: {data}")
            return []
        chat_ids = set()
        for update in data.get("result", []):
            # Private messages
            msg = update.get("message") or update.get("edited_message") or update.get("channel_post")
            if msg and msg.get("chat"):
                chat_ids.add(msg["chat"]["id"])
        chat_ids = list(chat_ids)
        if chat_ids:
            print(f"[Telegram] Auto-discovered chat IDs: {chat_ids}")
        else:
            print(f"[Telegram] No chats yet. Send /start to your bot once.")
        _TG_CHAT_IDS = chat_ids
        # Cache to file for this run
        try:
            _TG_CHAT_CACHE.write_text(",".join(str(c) for c in chat_ids))
        except Exception:
            pass
        return chat_ids
    except Exception as e:
        print(f"[Telegram] discovery error: {e}")
        return []


def _tg_get_chat_ids():
    """Get chat IDs — use cached or discover."""
    global _TG_CHAT_IDS
    if _TG_CHAT_IDS:
        return _TG_CHAT_IDS
    # Try cache first (within same run)
    if _TG_CHAT_CACHE.exists():
        try:
            cached = _TG_CHAT_CACHE.read_text().strip()
            if cached:
                _TG_CHAT_IDS = [int(c) for c in cached.split(",")]
                return _TG_CHAT_IDS
        except Exception:
            pass
    # Try explicit env var (if user set it)
    env_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if env_id:
        _TG_CHAT_IDS = [int(env_id)]
        return _TG_CHAT_IDS
    # Auto-discover
    return tg_discover_chat_ids()


def tg_send(message, silent=False):
    """Send Telegram message to all auto-discovered chats."""
    if not TELEGRAM_BOT_TOKEN:
        return
    chat_ids = _tg_get_chat_ids()
    if not chat_ids:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for cid in chat_ids:
        try:
            requests.post(url, json={
                "chat_id": cid,
                "text": message,
                "parse_mode": "HTML",
                "disable_notification": silent,
            }, timeout=10)
        except Exception as e:
            print(f"[Telegram error to {cid}] {e}")


def normalize_fingerprint(fp):
    if not fp:
        return fp
    fp = fp.strip().strip('"').strip("'").lower()
    fp = re.sub(r'[^0-9a-f:]', '', fp)
    if ':' not in fp and len(fp) == 32:
        fp = ':'.join(fp[i:i+2] for i in range(0, 32, 2))
    return fp


# ─────────────────────── READ OCI CONFIG ───────────────────────
oci_config_path = OCI_CONFIG if OCI_CONFIG else os.path.expanduser("~/.oci/config")
cfg_parser = configparser.ConfigParser()
cfg_parser.read(oci_config_path)

try:
    key_file_path = os.path.expanduser(cfg_parser.get('DEFAULT', 'key_file').strip())
    raw_tenancy = cfg_parser.get('DEFAULT', 'tenancy').strip()
    raw_user = cfg_parser.get('DEFAULT', 'user').strip()
    raw_fingerprint = normalize_fingerprint(cfg_parser.get('DEFAULT', 'fingerprint'))
    raw_region = cfg_parser.get('DEFAULT', 'region').strip()
except (configparser.Error, configparser.NoOptionError) as e:
    print(f"[Config Error] {e}")
    sys.exit(1)

OCI_USER_ID = raw_user

print(f"[Config check] tenancy: {raw_tenancy[:30]}... (len={len(raw_tenancy)})")
print(f"[Config check] user: {raw_user[:30]}... (len={len(raw_user)})")
print(f"[Config check] fingerprint: {raw_fingerprint} (len={len(raw_fingerprint)})")
print(f"[Config check] region: {raw_region}")
print(f"[Config check] key_file: {key_file_path} (exists: {os.path.exists(key_file_path)})")

if os.path.exists(key_file_path):
    with open(key_file_path, 'r') as f:
        first_line = f.readline().strip()
    print(f"[Config check] key_file first line: {first_line}")
    if 'PRIVATE' not in first_line:
        print("[FATAL] Not a private key!")
        tg_send("❌ <b>FATAL:</b> OCI_KEY_B64 me PRIVATE key nahi hai. Re-upload correct .pem")
        sys.exit(1)

# Discover telegram chats early (so we can notify about setup issues)
if TELEGRAM_BOT_TOKEN:
    tg_discover_chat_ids()
    if not _TG_CHAT_IDS:
        print("[Telegram] ⚠️ No chat IDs. Open https://t.me/Newservertry_bot and send /start")

# Logging
logging.basicConfig(filename="setup_and_info.log", level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logging_step5 = logging.getLogger("launch_instance")
logging_step5.setLevel(logging.INFO)
fh = logging.FileHandler("launch_instance.log")
fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logging_step5.addHandler(fh)

# ─────────────────────── BUILD OCI CLIENTS ───────────────────────
client_config = {
    "user": raw_user,
    "fingerprint": raw_fingerprint,
    "tenancy": raw_tenancy,
    "region": raw_region,
    "key_file": key_file_path,
}

try:
    oci.config.validate_config(client_config)
    print("[Config check] validate_config: PASSED ✓")
except oci.exceptions.InvalidConfig as cfg_err:
    print(f"[FATAL] validate_config failed: {cfg_err}")
    tg_send(f"❌ Config invalid: {cfg_err}")
    sys.exit(1)

iam_client = oci.identity.IdentityClient(client_config)
network_client = oci.core.VirtualNetworkClient(client_config)
compute_client = oci.core.ComputeClient(client_config)


# ─────────────────────── HELPERS ───────────────────────
def write_into_file(file_path, data):
    with open(file_path, mode="a", encoding="utf-8") as f:
        f.write(data)


def list_all_instances(compartment_id):
    return compute_client.list_instances(compartment_id=compartment_id).data


def generate_ssh_key_pair(public_key_file, private_key_file):
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(private_key_file)
    write_into_file(public_key_file, f"ssh-rsa {key.get_base64()} auto_generated")


def read_or_generate_ssh_public_key(public_key_file):
    p = Path(public_key_file)
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        generate_ssh_key_pair(p, p.with_name(f"{p.stem}_private"))
    return p.read_text(encoding="utf-8")


def celebrate_success(instance, public_ip=None):
    """5 celebration messages on Telegram."""
    tg_send("🎉🎉🎉 <b>SUCCESS!</b> 🎉🎉🎉")
    time.sleep(1)
    tg_send(f"✅ <b>ARM Instance Created!</b>\n\n"
            f"📛 Name: <code>{instance.display_name}</code>\n"
            f"🆔 ID: <code>{instance.id}</code>\n"
            f"🌐 AD: <code>{instance.availability_domain}</code>\n"
            f"⚙️ Shape: <code>{instance.shape}</code>\n"
            f"📊 State: <code>{instance.lifecycle_state}</code>")
    time.sleep(1)
    if public_ip:
        tg_send(f"🌍 <b>Public IP:</b> <code>{public_ip}</code>")
        time.sleep(1)
        tg_send(f"💻 <b>SSH Command:</b>\n<code>ssh -i your_private_key ubuntu@{public_ip}</code>")
    else:
        tg_send("🌍 Check OCI Console → Compute → Instances for public IP")
    time.sleep(1)
    tg_send(f"💾 <b>{int(OCI_OCPUS)} OCPU / {int(OCI_MEMORY_GB)} GB RAM / {BOOT_VOLUME_SIZE} GB Storage</b>\n\n"
            f"🚀 Server ab tera hai! Enjoy! 🚀")


def create_instance_details_file(instance, public_ip=None):
    details = [f"Instance ID: {instance.id}",
               f"Display Name: {instance.display_name}",
               f"Availability Domain: {instance.availability_domain}",
               f"Shape: {instance.shape}",
               f"State: {instance.lifecycle_state}"]
    if public_ip:
        details.append(f"Public IP: {public_ip}")
    write_into_file('INSTANCE_CREATED', '\n'.join(details) + '\n')
    celebrate_success(instance, public_ip)


def check_instance_state_and_write(compartment_id, shape, states=('RUNNING', 'PROVISIONING'), tries=3):
    for _ in range(tries):
        instance_list = list_all_instances(compartment_id=compartment_id)
        if shape == ARM_SHAPE:
            arm = next((i for i in instance_list if i.shape == shape and i.lifecycle_state in states), None)
            if arm:
                pub_ip = None
                try:
                    vnic_attachments = compute_client.list_vnic_attachments(
                        compartment_id=compartment_id, instance_id=arm.id).data
                    if vnic_attachments:
                        vnic = network_client.get_vnic(vnic_attachments[0].vnic_id).data
                        pub_ip = vnic.public_ip
                except Exception:
                    pass
                create_instance_details_file(arm, pub_ip)
                return True
        if tries - 1 > 0:
            time.sleep(60)
    return False


def handle_errors(command, data, log):
    code = data.get("code", "")
    status = data.get("status")
    msg = data.get("message", "")

    if code in ("TooManyRequests", "Out of host capacity.", 'InternalError') \
            or msg in ("Out of host capacity.", "Bad Gateway"):
        log.info("Transient: %s - %s", code, msg)
        sleep_time = max(WAIT_TIME, 120) if (code == "TooManyRequests" or status == 429) else WAIT_TIME
        time.sleep(sleep_time)
        return True

    if status == 502:
        time.sleep(WAIT_TIME)
        return True

    if status == 401 or code == "NotAuthenticated":
        err = ("❌ <b>401 Auth Failed</b>\n\nFingerprint ya private key OCI me uploaded key se "
               "match nahi kar rahi. OCI Console → API Keys me fingerprint check karo.")
        print(err)
        tg_send(err)
        sys.exit(1)

    failure = '\n'.join([f'{k}: {v}' for k, v in data.items()])
    tg_send(f"❌ <b>Error:</b>\n<pre>{failure[:500]}</pre>")
    raise Exception(f"Error: {data}")


def execute_oci_command(client, method, *args, **kwargs):
    while True:
        try:
            response = getattr(client, method)(*args, **kwargs)
            return response.data if hasattr(response, "data") else response
        except oci.exceptions.ServiceError as srv_err:
            data = {"status": srv_err.status, "code": srv_err.code, "message": srv_err.message}
            handle_errors(args, data, logging_step5)
        except (oci.exceptions.RequestException, ConnectionError, OSError) as conn_err:
            logging_step5.error("Network error in %s: %s", method, str(conn_err))
            time.sleep(WAIT_TIME)


# ─────────────────────── MAIN ───────────────────────
def launch_instance():
    user_info = execute_oci_command(iam_client, "get_user", OCI_USER_ID)
    oci_tenancy = user_info.compartment_id
    print(f"[Auth OK] Connected to tenancy: {oci_tenancy[:40]}...")

    availability_domains = execute_oci_command(iam_client, "list_availability_domains",
                                               compartment_id=oci_tenancy)
    if OCT_FREE_AD:
        oci_ad_name = [i.name for i in availability_domains if
                       any(i.name.endswith(a) for a in OCT_FREE_AD.split(","))]
    else:
        oci_ad_name = [i.name for i in availability_domains]
    oci_ad_names = itertools.cycle(oci_ad_name)
    print(f"[ADs] Will try: {oci_ad_name}")

    oci_subnet_id = OCI_SUBNET_ID
    if not oci_subnet_id:
        subnets = execute_oci_command(network_client, "list_subnets", compartment_id=oci_tenancy)
        oci_subnet_id = subnets[0].id

    if not OCI_IMAGE_ID:
        images = execute_oci_command(compute_client, "list_images",
                                     compartment_id=oci_tenancy, shape=OCI_COMPUTE_SHAPE)
        oci_image_id = next(i.id for i in images if
                            i.operating_system == OPERATING_SYSTEM and
                            i.operating_system_version == OS_VERSION)
    else:
        oci_image_id = OCI_IMAGE_ID

    assign_public_ip = ASSIGN_PUBLIC_IP.lower() in ["true", "1", "y", "yes"]
    boot_volume_size = max(50, int(BOOT_VOLUME_SIZE))
    ssh_public_key = read_or_generate_ssh_public_key(SSH_AUTHORIZED_KEYS_FILE)

    if check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE, tries=1):
        return

    if OCI_COMPUTE_SHAPE == ARM_SHAPE:
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCI_OCPUS, memory_in_gbs=OCI_MEMORY_GB)
        print(f"[Shape] Requesting {OCI_OCPUS} OCPU / {OCI_MEMORY_GB} GB RAM")
    else:
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=1)

    attempt = 0
    while True:
        attempt += 1
        current_ad = next(oci_ad_names)
        print(f"[Attempt #{attempt}] Trying AD: {current_ad}")
        try:
            resp = execute_oci_command(
                compute_client, "launch_instance",
                launch_instance_details=oci.core.models.LaunchInstanceDetails(
                    availability_domain=current_ad,
                    compartment_id=oci_tenancy,
                    create_vnic_details=oci.core.models.CreateVnicDetails(
                        assign_public_ip=assign_public_ip,
                        assign_private_dns_record=True,
                        display_name=DISPLAY_NAME,
                        subnet_id=oci_subnet_id,
                    ),
                    display_name=DISPLAY_NAME,
                    shape=OCI_COMPUTE_SHAPE,
                    availability_config=oci.core.models.LaunchInstanceAvailabilityConfigDetails(
                        recovery_action="RESTORE_INSTANCE"),
                    instance_options=oci.core.models.InstanceOptions(
                        are_legacy_imds_endpoints_disabled=False),
                    shape_config=shape_config,
                    source_details=oci.core.models.InstanceSourceViaImageDetails(
                        source_type="image", image_id=oci_image_id,
                        boot_volume_size_in_gbs=boot_volume_size),
                    metadata={"ssh_authorized_keys": ssh_public_key},
                )
            )
            if resp:
                print(f"[SUCCESS] Instance launch initiated!")
                if check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE):
                    return
        except oci.exceptions.ServiceError as srv_err:
            if srv_err.code == "LimitExceeded":
                if check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE):
                    return
            data = {"status": srv_err.status, "code": srv_err.code, "message": srv_err.message}
            handle_errors("launch_instance", data, logging_step5)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()

    # Status ping every ~30 min (not every 5 min to avoid spam)
    from datetime import datetime
    now = datetime.utcnow()
    if now.minute < 5 or (25 <= now.minute < 30):
        tg_send(f"🔄 ARM instance attempt at {now.strftime('%H:%M UTC')} — "
                f"trying {int(OCI_OCPUS)} OCPU / {int(OCI_MEMORY_GB)} GB...", silent=True)

    try:
        launch_instance()
        if args.once:
            sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        tg_send(f"❌ <b>Script crashed:</b>\n<pre>{str(e)[:500]}</pre>")
        raise
