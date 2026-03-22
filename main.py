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

def _run_fast_cmd(client, cmd):
    """Run a command without waiting for full environment load to speed up execution"""
    # For simple commands like eval echo or mkdir, we don't need a full shell
    # But some servers have heavy .bashrc that slows down exec_command
    # We can try to run it directly if possible, or accept the cost
    stdin, stdout, stderr = client.exec_command(cmd)
    return stdin, stdout, stderr

def sync_and_run(host, port, username, password, local_root, remote_root, files_to_sync, run_cmd, screen_session=None):
    start_time = time.time()
    print(f"Connecting to {host}:{port} as {username}...")
    try:
        # Create SSH client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Connect with a timeout
        client.connect(host, port=port, username=username, password=password, timeout=10)
        print(f"Connected successfully! (Took {time.time() - start_time:.2f}s)")
        
        # Open SFTP
        sftp = client.open_sftp()
        
        # Sync Files
        print("\nSyncing files...")
        sync_start_time = time.time()
        
        # Resolve remote root once. 
        # We use sftp.normalize to resolve paths instead of slow bash eval where possible
        try:
            # If remote_root starts with ~, sftp.normalize('.') gets the home dir
            if remote_root.startswith('~/'):
                home_dir = sftp.normalize('.')
                resolved_remote_root = f"{home_dir}/{remote_root[2:]}"
            elif remote_root == '~':
                resolved_remote_root = sftp.normalize('.')
            else:
                resolved_remote_root = remote_root
        except Exception:
            # Fallback to bash eval if sftp normalize fails
            stdin, stdout, stderr = client.exec_command(f"eval echo \"{remote_root}\"")
            resolved_remote_root = stdout.read().decode().strip()
            
        if not resolved_remote_root:
            print(f"Error: Could not resolve remote_root '{remote_root}'")
            return
            
        # Instead of calling mkdir -p via SSH which is slow on some servers, 
        # let's create directories via SFTP which is much faster
        def ensure_remote_dir_sftp(sftp_client, remote_directory):
            if remote_directory == '/':
                return
            try:
                sftp_client.stat(remote_directory)
            except IOError:
                # Directory doesn't exist, create parent first
                parent_dir = os.path.dirname(remote_directory)
                if parent_dir and parent_dir != remote_directory:
                    ensure_remote_dir_sftp(sftp_client, parent_dir)
                try:
                    sftp_client.mkdir(remote_directory)
                except IOError as e:
                    pass # Ignore if it already exists or we can't create it
                    
        ensure_remote_dir_sftp(sftp, resolved_remote_root)

        # Keep track of created directories to avoid redundant stat calls
        created_dirs = {resolved_remote_root}

        for rel_path in files_to_sync:
            local_path = os.path.join(local_root, rel_path)
            # Ensure consistent forward slashes for remote path
            remote_path = f"{resolved_remote_root}/{rel_path}".replace('\\', '/').replace('//', '/')
            
            if not os.path.exists(local_path):
                print(f"Local file not found, skipping: {local_path}")
                continue
                
            # Ensure remote directory exists
            remote_dir = os.path.dirname(remote_path)
            if remote_dir and remote_dir not in created_dirs:
                ensure_remote_dir_sftp(sftp, remote_dir)
                created_dirs.add(remote_dir)
            
            print(f"Uploading {local_path} -> {remote_path}")
            try:
                # Direct SFTP put using resolved absolute path is much faster than running touch/eval per file
                sftp.put(local_path, remote_path)
            except Exception as e:
                print(f"SFTP failed: {e}. Trying fallback upload...")
                try:
                    # Fallback using base64 encoded echo
                    with open(local_path, 'rb') as f:
                        content = f.read()
                    import base64
                    encoded_content = base64.b64encode(content).decode()
                    # Resolve remote path before writing
                    cmd = f"resolved_path=$(eval echo \"{remote_path}\") && echo {encoded_content} | base64 -d > \"$resolved_path\""
                    stdin, stdout, stderr = client.exec_command(cmd)
                    if stdout.channel.recv_exit_status() != 0:
                         print(f"Failed to upload via base64: {stderr.read().decode()}")
                    else:
                         print("Fallback upload successful.")
                except Exception as ex:
                    print(f"Failed to upload {local_path} completely: {ex}")
        
        sftp.close()
        print(f"Sync completed! (Took {time.time() - sync_start_time:.2f}s)")
        
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
            
            # Resolve remote_root so that things like ~ expand properly in the script
            wrapped_cmd = f"cd $(eval echo \"{remote_root}\") && ({run_cmd}) > {log_file} 2>&1; echo {end_marker} >> {log_file}"
            
            # To avoid newline and escaping issues with screen's stuff command, 
            # we write the command to a temporary bash script and execute that script in the screen.
            script_file = f"/tmp/use_ai_server_script_{uuid.uuid4().hex}.sh"
            
            import base64
            encoded_script = base64.b64encode(wrapped_cmd.encode()).decode()
            
            # Create the script file on the remote server
            _run_fast_cmd(client, f"echo {encoded_script} | base64 -d > '{script_file}' && chmod +x '{script_file}'")
            
            # Send the execution command to screen. Using $'\n' ensures a literal enter key is passed in bash.
            full_cmd = f"screen -S {screen_session} -X stuff 'bash {script_file}'$'\n'"
            print(f"Sending command to screen: {full_cmd}")
            
            stdin, stdout, stderr = _run_fast_cmd(client, full_cmd)
            
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
                _run_fast_cmd(client, f"pkill -f 'tail -f {log_file}'") 
                _run_fast_cmd(client, f"rm -f {log_file} {script_file}")
                
            print("-" * 40)
            print("\nCommand execution in screen session completed (or log tailing stopped).")
        else:
            print("No screen session specified.")
                
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
