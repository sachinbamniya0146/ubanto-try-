import configparser
import itertools
import json
import logging
import os
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Union

import oci
import paramiko
from dotenv import load_dotenv
import requests

# Load environment variables from .env file
load_dotenv('oci.env')

ARM_SHAPE = "VM.Standard.A1.Flex"
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"

# Access loaded environment variables and strip white spaces
OCI_CONFIG = os.path.expanduser(os.getenv("OCI_CONFIG", "").strip())
OCT_FREE_AD = os.getenv("OCT_FREE_AD", "").strip()
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "").strip()
WAIT_TIME = int(os.getenv("REQUEST_WAIT_TIME_SECS", "0").strip())
SSH_AUTHORIZED_KEYS_FILE = os.path.expanduser(os.getenv("SSH_AUTHORIZED_KEYS_FILE", "").strip())
OCI_IMAGE_ID = os.getenv("OCI_IMAGE_ID", None).strip() if os.getenv("OCI_IMAGE_ID") else None
OCI_COMPUTE_SHAPE = os.getenv("OCI_COMPUTE_SHAPE", ARM_SHAPE).strip()
SECOND_MICRO_INSTANCE = os.getenv("SECOND_MICRO_INSTANCE", 'False').strip().lower() == 'true'
OCI_SUBNET_ID = os.getenv("OCI_SUBNET_ID", None).strip() if os.getenv("OCI_SUBNET_ID") else None
OPERATING_SYSTEM = os.getenv("OPERATING_SYSTEM", "").strip()
OS_VERSION = os.getenv("OS_VERSION", "").strip()
ASSIGN_PUBLIC_IP = os.getenv("ASSIGN_PUBLIC_IP", "false").strip()
BOOT_VOLUME_SIZE = os.getenv("BOOT_VOLUME_SIZE", "50").strip()
# >>> USER-CONFIGURABLE SHAPE (defaults: 2 OCPU + 12 GB RAM for ARM) <<<
OCI_OCPUS = float(os.getenv("OCI_OCPUS", "2").strip())
OCI_MEMORY_GB = float(os.getenv("OCI_MEMORY_GB", "12").strip())
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", 'False').strip().lower() == 'true'
EMAIL = os.getenv("EMAIL", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def normalize_fingerprint(fp):
    """Normalize fingerprint: strip whitespace, lowercase, fix missing colons."""
    if not fp:
        return fp
    # Remove all whitespace, quotes, and anything non-hex-non-colon
    fp = fp.strip().strip('"').strip("'").lower()
    fp = re.sub(r'[^0-9a-f:]', '', fp)
    # If exactly 32 hex chars with no colons, insert colons every 2 chars
    if ':' not in fp and len(fp) == 32:
        fp = ':'.join(fp[i:i+2] for i in range(0, 32, 2))
    return fp


# Read the configuration from oci_config file
config = configparser.ConfigParser()
OCI_USER_ID = None
try:
    config.read(OCI_CONFIG)
    OCI_USER_ID = config.get('DEFAULT', 'user').strip()
    if OCI_COMPUTE_SHAPE not in (ARM_SHAPE, E2_MICRO_SHAPE):
        raise ValueError(f"{OCI_COMPUTE_SHAPE} is not an acceptable shape")

except configparser.Error as e:
    with open("ERROR_IN_CONFIG.log", "w", encoding='utf-8') as file:
        file.write(str(e))
    print(f"Error reading the configuration file: {e}")
    sys.exit(1)

if not OCI_USER_ID:
    print("Error: OCI_USER_ID not found in config")
    sys.exit(1)

# Set up logging
logging.basicConfig(
    filename="setup_and_info.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logging_step5 = logging.getLogger("launch_instance")
logging_step5.setLevel(logging.INFO)
fh = logging.FileHandler("launch_instance.log")
fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logging_step5.addHandler(fh)

# ==========================================================================
# >>> FIX v2: Use Signer to BYPASS local fingerprint validation <<<
# OCI SDK's validate_config() rejects "malformed" fingerprints locally.
# Signer approach: build Signer with raw fingerprint, pass minimal config
# (only region). SDK then only validates region. If fingerprint is truly
# wrong, API server returns 401 — we catch that with a clear message.
# ==========================================================================
oci_config_path = OCI_CONFIG if OCI_CONFIG else os.path.expanduser("~/.oci/config")
config = configparser.ConfigParser()
config.read(oci_config_path)

key_file_path = os.path.expanduser(config.get('DEFAULT', 'key_file').strip())
raw_tenancy = config.get('DEFAULT', 'tenancy').strip()
raw_user = config.get('DEFAULT', 'user').strip()
raw_fingerprint = normalize_fingerprint(config.get('DEFAULT', 'fingerprint'))
raw_region = config.get('DEFAULT', 'region').strip()

# Diagnostic print (safe — just lengths and partial values, no secrets)
print(f"[Config check] tenancy starts with: {raw_tenancy[:20]}... (len={len(raw_tenancy)})")
print(f"[Config check] user starts with: {raw_user[:20]}... (len={len(raw_user)})")
print(f"[Config check] fingerprint: {raw_fingerprint} (len={len(raw_fingerprint)})")
print(f"[Config check] region: {raw_region}")
print(f"[Config check] key_file path: {key_file_path} (exists: {os.path.exists(key_file_path)})")

# Build Signer directly — this does NOT call validate_config
try:
    signer = oci.signer.Signer(
        tenancy=raw_tenancy,
        user=raw_user,
        fingerprint=raw_fingerprint,
        private_key_file_location=key_file_path,
    )
except Exception as e:
    print(f"Signer creation failed: {e}")
    with open("ERROR_IN_CONFIG.log", "w", encoding='utf-8') as file:
        file.write(f"Signer creation failed: {e}")
    sys.exit(1)

# Minimal config — ONLY region. Signer handles auth.
# With signer passed, validate_config() only checks for 'region'.
client_config = {"region": raw_region}

iam_client = oci.identity.IdentityClient(client_config, signer=signer)
network_client = oci.core.VirtualNetworkClient(client_config, signer=signer)
compute_client = oci.core.ComputeClient(client_config, signer=signer)

IMAGE_LIST_KEYS = [
    "lifecycle_state",
    "display_name",
    "id",
    "operating_system",
    "operating_system_version",
    "size_in_mbs",
    "time_created",
]


def write_into_file(file_path, data):
    with open(file_path, mode="a", encoding="utf-8") as file_writer:
        file_writer.write(data)


def send_email(subject, body, email, password):
    message = MIMEMultipart()
    message["Subject"] = subject
    message["From"] = email
    message["To"] = email
    html_body = MIMEText(body, "html")
    message.attach(html_body)
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        try:
            server.starttls()
            server.login(email, password)
            server.sendmail(email, email, message.as_string())
        except smtplib.SMTPException as mail_err:
            logging.error("Error while sending email: %s", mail_err)
            raise


def list_all_instances(compartment_id):
    list_instances_response = compute_client.list_instances(compartment_id=compartment_id)
    return list_instances_response.data


def generate_html_body(instance):
    with open('email_content.html', 'r', encoding='utf-8') as email_temp:
        html_template = email_temp.read()
    html_body = html_template.replace('&lt;INSTANCE_ID&gt;', instance.id)
    html_body = html_body.replace('&lt;DISPLAY_NAME&gt;', instance.display_name)
    html_body = html_body.replace('&lt;AD&gt;', instance.availability_domain)
    html_body = html_body.replace('&lt;SHAPE&gt;', instance.shape)
    html_body = html_body.replace('&lt;STATE&gt;', instance.lifecycle_state)
    return html_body


def create_instance_details_file_and_notify(instance, shape=ARM_SHAPE):
    details = [f"Instance ID: {instance.id}",
               f"Display Name: {instance.display_name}",
               f"Availability Domain: {instance.availability_domain}",
               f"Shape: {instance.shape}",
               f"State: {instance.lifecycle_state}",
               "\n"]
    micro_body = 'Two Micro Instances are already existing and running'
    arm_body = '\n'.join(details)
    body = arm_body if shape == ARM_SHAPE else micro_body
    write_into_file('INSTANCE_CREATED', body)

    try:
        html_body = generate_html_body(instance)
    except FileNotFoundError:
        html_body = arm_body

    if NOTIFY_EMAIL:
        send_email('OCI INSTANCE CREATED', html_body, EMAIL, EMAIL_PASSWORD)


def notify_on_failure(failure_msg):
    mail_body = (
        "The script encountered an unhandled error and exited unexpectedly.\n\n"
        f"{failure_msg}"
    )
    write_into_file('UNHANDLED_ERROR.log', mail_body)
    if NOTIFY_EMAIL:
        send_email('OCI INSTANCE CREATION SCRIPT: FAILED', mail_body, EMAIL, EMAIL_PASSWORD)


def check_instance_state_and_write(compartment_id, shape, states=('RUNNING', 'PROVISIONING'),
                                   tries=3):
    for _ in range(tries):
        instance_list = list_all_instances(compartment_id=compartment_id)
        if shape == ARM_SHAPE:
            running_arm_instance = next((instance for instance in instance_list if
                                         instance.shape == shape and instance.lifecycle_state in states), None)
            if running_arm_instance:
                create_instance_details_file_and_notify(running_arm_instance, shape)
                return True
        else:
            micro_instance_list = [instance for instance in instance_list if
                                   instance.shape == shape and instance.lifecycle_state in states]
            if len(micro_instance_list) > 1 and SECOND_MICRO_INSTANCE:
                create_instance_details_file_and_notify(micro_instance_list[-1], shape)
                return True
            if len(micro_instance_list) == 1 and not SECOND_MICRO_INSTANCE:
                create_instance_details_file_and_notify(micro_instance_list[-1], shape)
                return True
        if tries - 1 > 0:
            time.sleep(60)
    return False


def handle_errors(command, data, log):
    if "code" in data:
        if (data["code"] in ("TooManyRequests", "Out of host capacity.", 'InternalError')) \
                or (data["message"] in ("Out of host capacity.", "Bad Gateway")):
            log.info("Command: %s--\nOutput: %s", command, data)
            if data.get("code") == "TooManyRequests" or data.get("status") == 429:
                sleep_time = max(WAIT_TIME, 120)
            else:
                sleep_time = WAIT_TIME
            log.info("Sleeping %s sec before next attempt...", sleep_time)
            time.sleep(sleep_time)
            return True

    if "status" in data and data["status"] == 502:
        log.info("Command: %s~~\nOutput: %s", command, data)
        time.sleep(WAIT_TIME)
        return True
    # 401 = bad fingerprint / key mismatch — permanent, not retry-able
    if data.get("status") == 401 or data.get("code") == "NotAuthenticated":
        msg = ("Authentication failed (401). Likely cause: fingerprint in OCI_CONFIG_B64 "
               "does not match the API key you uploaded in OCI Console. "
               "Check: OCI Console → Profile → API Keys → fingerprint column, "
               "copy that EXACT value into your config file's `fingerprint=` line, "
               "then re-run: base64 -w 0 ~/.oci/config  and update OCI_CONFIG_B64 secret.")
        log.error(msg)
        print(msg)
        notify_on_failure(msg)
        sys.exit(1)
    failure_msg = '\n'.join([f'{key}: {value}' for key, value in data.items()])
    notify_on_failure(failure_msg)
    raise Exception("Error: %s" % data)


def execute_oci_command(client, method, *args, **kwargs):
    while True:
        try:
            response = getattr(client, method)(*args, **kwargs)
            data = response.data if hasattr(response, "data") else response
            return data
        except oci.exceptions.ServiceError as srv_err:
            data = {"status": srv_err.status,
                    "code": srv_err.code,
                    "message": srv_err.message}
            handle_errors(args, data, logging_step5)
        except (oci.exceptions.RequestException, ConnectionError, OSError) as conn_err:
            logging_step5.error("Network/connection error in %s: %s", method, str(conn_err))
            time.sleep(WAIT_TIME)


def generate_ssh_key_pair(public_key_file, private_key_file):
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(private_key_file)
    write_into_file(public_key_file, (f"ssh-rsa {key.get_base64()} "
                                      f"{Path(public_key_file).stem}_auto_generated"))


def read_or_generate_ssh_public_key(public_key_file):
    public_key_path = Path(public_key_file)
    if not public_key_path.is_file():
        logging.info("SSH key doesn't exist... Generating SSH Key Pair")
        public_key_path.parent.mkdir(parents=True, exist_ok=True)
        private_key_path = public_key_path.with_name(f"{public_key_path.stem}_private")
        generate_ssh_key_pair(public_key_path, private_key_path)
    with open(public_key_path, "r", encoding="utf-8") as pub_key_file:
        ssh_public_key = pub_key_file.read()
    return ssh_public_key


def send_discord_message(message):
    if DISCORD_WEBHOOK:
        payload = {"content": message}
        try:
            response = requests.post(DISCORD_WEBHOOK, json=payload)
            response.raise_for_status()
        except requests.RequestException as e:
            logging.error("Failed to send Discord message: %s", e)


def send_telegram_message(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
        except requests.RequestException as e:
            logging.error("Failed to send Telegram message: %s", e)


def notify_all(message):
    send_discord_message(message)
    send_telegram_message(message)


def launch_instance():
    # Step 1 - Get TENANCY (first live API call — proves auth works)
    user_info = execute_oci_command(iam_client, "get_user", OCI_USER_ID)
    oci_tenancy = user_info.compartment_id
    logging.info("OCI_TENANCY: %s", oci_tenancy)
    print(f"[Auth OK] Connected to tenancy: {oci_tenancy}")

    # Step 2 - Get AD Name — try ALL ADs for best capacity chance
    availability_domains = execute_oci_command(iam_client,
                                               "list_availability_domains",
                                               compartment_id=oci_tenancy)
    if OCT_FREE_AD:
        oci_ad_name = [item.name for item in availability_domains if
                       any(item.name.endswith(oct_ad) for oct_ad in OCT_FREE_AD.split(","))]
    else:
        oci_ad_name = [item.name for item in availability_domains]
    oci_ad_names = itertools.cycle(oci_ad_name)
    logging.info("OCI_AD_NAME: %s", oci_ad_name)
    print(f"[ADs] Will try: {oci_ad_name}")

    # Step 3 - Get Subnet ID
    oci_subnet_id = OCI_SUBNET_ID
    if not oci_subnet_id:
        subnets = execute_oci_command(network_client,
                                      "list_subnets",
                                      compartment_id=oci_tenancy)
        oci_subnet_id = subnets[0].id
    logging.info("OCI_SUBNET_ID: %s", oci_subnet_id)

    # Step 4 - Get Image ID of Compute Shape
    if not OCI_IMAGE_ID:
        images = execute_oci_command(
            compute_client,
            "list_images",
            compartment_id=oci_tenancy,
            shape=OCI_COMPUTE_SHAPE,
        )
        shortened_images = [{key: json.loads(str(image))[key] for key in IMAGE_LIST_KEYS
                             } for image in images]
        write_into_file('images_list.json', json.dumps(shortened_images, indent=2))
        oci_image_id = next(image.id for image in images if
                            image.operating_system == OPERATING_SYSTEM and
                            image.operating_system_version == OS_VERSION)
        logging.info("OCI_IMAGE_ID: %s", oci_image_id)
    else:
        oci_image_id = OCI_IMAGE_ID

    assign_public_ip = ASSIGN_PUBLIC_IP.lower() in ["true", "1", "y", "yes"]
    boot_volume_size = max(50, int(BOOT_VOLUME_SIZE))
    ssh_public_key = read_or_generate_ssh_public_key(SSH_AUTHORIZED_KEYS_FILE)

    # Step 5 - Launch Instance if not already running
    instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE, tries=1)

    # Shape config — defaults 2 OCPU / 12 GB; override via oci.env
    if OCI_COMPUTE_SHAPE == "VM.Standard.A1.Flex":
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCI_OCPUS, memory_in_gbs=OCI_MEMORY_GB
        )
        logging.info("ARM shape_config: ocpus=%s memory=%s GB", OCI_OCPUS, OCI_MEMORY_GB)
        print(f"[Shape] Requesting {OCI_OCPUS} OCPU / {OCI_MEMORY_GB} GB RAM")
    else:
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=1)

    while not instance_exist_flag:
            try:
                launch_instance_response = execute_oci_command(
                    compute_client,
                    "launch_instance",
                    launch_instance_details=oci.core.models.LaunchInstanceDetails(
                        availability_domain=next(oci_ad_names),
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
                            recovery_action="RESTORE_INSTANCE"
                        ),
                        instance_options=oci.core.models.InstanceOptions(
                            are_legacy_imds_endpoints_disabled=False
                        ),
                        shape_config=shape_config,
                        source_details=oci.core.models.InstanceSourceViaImageDetails(
                            source_type="image",
                            image_id=oci_image_id,
                            boot_volume_size_in_gbs=boot_volume_size,
                        ),
                        metadata={"ssh_authorized_keys": ssh_public_key},
                    )
                )
                if launch_instance_response:
                    logging_step5.info(
                        "Command: launch_instance\nOutput: %s", launch_instance_response
                    )
                    instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE)

            except oci.exceptions.ServiceError as srv_err:
                if srv_err.code == "LimitExceeded":
                    logging_step5.info("LimitExceeded code:%s message:%s status:%s",
                                       srv_err.code, srv_err.message, srv_err.status)
                    instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE)
                    if instance_exist_flag:
                        logging_step5.info("%s , exiting the program", srv_err.code)
                        sys.exit()
                    logging_step5.info("Didn't find an instance , proceeding with retries")
                data = {
                    "status": srv_err.status,
                    "code": srv_err.code,
                    "message": srv_err.message,
                }
                handle_errors("launch_instance", data, logging_step5)

            except Exception as e:
                logging_step5.error("Network error in launch_instance: %s", str(e))
                time.sleep(WAIT_TIME)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Run once and exit (for GitHub Actions)')
    args = parser.parse_args()

    notify_all("🚀 OCI Instance Creation: Starting up!")
    try:
        launch_instance()
        notify_all("🎉 Success! OCI Instance has been created.")
        if args.once:
            print("Instance created successfully, exiting.")
            sys.exit(0)
    except Exception as e:
        error_message = f"😱 OCI Instance Creation failed:\n{str(e)}"
        notify_all(error_message)
        raise
