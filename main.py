import os
import sys
import time
import argparse
import yaml
from dotenv import load_dotenv
import paramiko

def load_config(config_path):
    if not os.path.exists(config_path):
        print(f"Error: Configuration file not found at {config_path}")
        sys.exit(1)
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def sync_and_run(host, port, username, password, local_root, remote_root, files_to_sync, run_cmd):
    print(f"Connecting to {host}:{port} as {username}...")
    try:
        # Create SSH client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Connect with a timeout
        client.connect(host, port=port, username=username, password=password, timeout=10)
        print("Connected successfully!")
        
        # Open SFTP
        sftp = client.open_sftp()
        
        # Sync Files
        print("\nSyncing files...")
        for rel_path in files_to_sync:
            local_path = os.path.join(local_root, rel_path)
            remote_path = remote_root + "/" + rel_path.replace("\\", "/") # Ensure unix style
            
            if not os.path.exists(local_path):
                print(f"Warning: Local file {local_path} does not exist, skipping.")
                continue

            # Ensure remote directory exists
            remote_dir = os.path.dirname(remote_path)
            try:
                sftp.stat(remote_dir)
            except FileNotFoundError:
                print(f"Creating remote directory: {remote_dir}")
                client.exec_command(f"mkdir -p {remote_dir}")
                time.sleep(0.5) # Wait for mkdir
            
            print(f"Uploading {local_path} -> {remote_path}")
            try:
                sftp.put(local_path, remote_path)
            except Exception as e:
                print(f"Failed to upload {local_path}: {e}")
        
        sftp.close()
        
        # Execute Command
        print(f"\nExecuting remote command: {run_cmd}")
        
        full_cmd = f"cd {remote_root} && {run_cmd} 2>&1"
        print(f"Full command: {full_cmd}")
        
        # Exec command
        stdin, stdout, stderr = client.exec_command(full_cmd, get_pty=False)
        
        # Stream output
        print("-" * 40)
        while not stdout.channel.exit_status_ready():
            if stdout.channel.recv_ready():
                output = stdout.channel.recv(1024).decode('utf-8', errors='ignore')
                sys.stdout.write(output)
                sys.stdout.flush()
            if stderr.channel.recv_ready():
                error = stderr.channel.recv(1024).decode('utf-8', errors='ignore')
                sys.stderr.write(error)
                sys.stderr.flush()
            time.sleep(0.1)
            
        # Final flush
        while stdout.channel.recv_ready():
            sys.stdout.write(stdout.channel.recv(1024).decode('utf-8', errors='ignore'))
        while stderr.channel.recv_ready():
            sys.stderr.write(stderr.channel.recv(1024).decode('utf-8', errors='ignore'))
            
        exit_status = stdout.channel.recv_exit_status()
        print("-" * 40)
        
        if exit_status == 0:
            print("\nCommand executed successfully!")
        else:
            print(f"\nCommand failed with exit code {exit_status}")
            
        client.close()
        
    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync files and run commands on a remote server.")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to configuration file")
    args = parser.parse_args()

    # Load env variables for credentials
    load_dotenv()
    
    config = load_config(args.config)
    
    # Credentials from env variables or config fallback
    HOST = os.getenv("REMOTE_HOST", config.get("server", {}).get("host"))
    PORT = int(os.getenv("REMOTE_PORT", config.get("server", {}).get("port", 22)))
    USER = os.getenv("REMOTE_USER", config.get("server", {}).get("user"))
    PASS = os.getenv("REMOTE_PASS", config.get("server", {}).get("password"))

    if not all([HOST, USER, PASS]):
        print("Error: Missing server credentials. Please check your .env file or config.yaml.")
        sys.exit(1)

    # Paths and Commands
    LOCAL_ROOT = config.get("sync", {}).get("local_root", ".")
    REMOTE_ROOT = config.get("sync", {}).get("remote_root", "~")
    FILES_TO_SYNC = config.get("sync", {}).get("files_to_sync", [])
    
    # Support for complex environment setup before running the main command
    ENV_SETUP = config.get("run", {}).get("env_setup", "")
    MAIN_CMD = config.get("run", {}).get("command", "")
    
    if ENV_SETUP:
        RUN_CMD = f"{ENV_SETUP} && {MAIN_CMD}"
    else:
        RUN_CMD = MAIN_CMD

    sync_and_run(HOST, PORT, USER, PASS, LOCAL_ROOT, REMOTE_ROOT, FILES_TO_SYNC, RUN_CMD)
