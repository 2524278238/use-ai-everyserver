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

def sync_and_run(host, port, username, password, local_root, remote_root, files_to_sync, run_cmd, screen_session=None):
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
        
        if screen_session:
            print(f"Targeting screen session: {screen_session}")
            # Ensure the screen session exists
            stdin, stdout, stderr = client.exec_command(f"screen -ls | grep {screen_session}")
            if stdout.channel.recv_exit_status() != 0:
                print(f"Warning: Screen session '{screen_session}' not found or not active.")
            
            # To get real-time logs, we can redirect the command's output to a temporary file
            # and then continuously tail that file in the current SSH session.
            import uuid
            log_file = f"/tmp/use_ai_server_{uuid.uuid4().hex}.log"
            
            # Send command to screen session via stuff
            # We wrap the run_cmd to redirect output to log_file, and add a unique completion marker
            end_marker = f"DONE_{uuid.uuid4().hex}"
            wrapped_cmd = f"cd {remote_root} && ({run_cmd}) > {log_file} 2>&1; echo {end_marker} >> {log_file}"
            
            # To avoid newline and escaping issues with screen's stuff command, 
            # we write the command to a temporary bash script and execute that script in the screen.
            script_file = f"/tmp/use_ai_server_script_{uuid.uuid4().hex}.sh"
            
            import base64
            encoded_script = base64.b64encode(wrapped_cmd.encode()).decode()
            
            # Create the script file on the remote server and WAIT for it to finish
            _, script_out, _ = client.exec_command(f"echo {encoded_script} | base64 -d > {script_file} && chmod +x {script_file}")
            script_out.channel.recv_exit_status()
            
            # Send the execution command to screen. Using $'\n' ensures a literal enter key is passed in bash.
            full_cmd = f"screen -S {screen_session} -X stuff 'bash {script_file}'$'\\n'"
            print(f"Sending command to screen: {full_cmd}")
            
            stdin, stdout, stderr = client.exec_command(full_cmd)
            exit_status = stdout.channel.recv_exit_status()
            
            if exit_status == 0:
                print(f"\nCommand successfully sent to screen session '{screen_session}'.")
                print("Tailing logs from the screen session...\n")
                print("-" * 40)
                
                # Start tailing the log file
                tail_cmd = f"touch {log_file} && tail -f {log_file}"
                tail_stdin, tail_stdout, tail_stderr = client.exec_command(tail_cmd, get_pty=False)
                
                # Stream output until the end marker is found
                try:
                    while True:
                        if tail_stdout.channel.recv_ready():
                            output = tail_stdout.channel.recv(1024).decode('utf-8', errors='ignore')
                            if end_marker in output:
                                # Print everything before the marker
                                output = output.replace(end_marker, "").strip()
                                if output:
                                    sys.stdout.write(output + "\n")
                                    sys.stdout.flush()
                                break
                            sys.stdout.write(output)
                            sys.stdout.flush()
                        time.sleep(0.1)
                except KeyboardInterrupt:
                    print("\nLog tailing interrupted by user.")
                finally:
                    # Clean up the tail process and the temporary files
                    # tail command might still be running. Easiest way to kill the specific tail is via pkill with full path
                    client.exec_command(f"pkill -f 'tail -f {log_file}'") 
                    client.exec_command(f"rm -f {log_file} {script_file}")
                
                print("-" * 40)
                print("\nCommand execution in screen session completed (or log tailing stopped).")
            else:
                print(f"\nFailed to send command to screen. Exit code {exit_status}")
                error_msg = stderr.read().decode('utf-8')
                if error_msg:
                    print(f"Error: {error_msg}")
            
        else:
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
    screen_session = config.get("run", {}).get("screen_session", "")
    ENV_SETUP = config.get("run", {}).get("env_setup", "")
    MAIN_CMD = config.get("run", {}).get("command", "")
    
    if ENV_SETUP:
        RUN_CMD = f"{ENV_SETUP} && {MAIN_CMD}"
    else:
        RUN_CMD = MAIN_CMD

    sync_and_run(HOST, PORT, USER, PASS, LOCAL_ROOT, REMOTE_ROOT, FILES_TO_SYNC, RUN_CMD, screen_session)
