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

def execute_shell_commands(channel, commands_list):
    """Execute multiple commands in an existing shell session to avoid channel startup delays."""
    output = ""
    # combine all commands with a unique exit marker
    import uuid
    marker = uuid.uuid4().hex
    
    full_script = "\n".join(commands_list)
    # the command string will have 'MARKER_' and 'END', but the output will be 'MARKER_...END'
    full_script += f"\necho 'MARKER_'\"{marker}\"'_END'\n"
    
    channel.send(full_script)
    
    # Wait for the marker
    expected_output_marker = f"MARKER_{marker}_END"
    while True:
        if channel.recv_ready():
            chunk = channel.recv(4096).decode('utf-8', errors='ignore')
            output += chunk
            # Print raw output for debugging
            # print("DEBUG SHELL CHUNK:", repr(chunk))
            if expected_output_marker in output:
                # Add a tiny sleep to make sure we got the rest of the line
                time.sleep(0.1)
                if channel.recv_ready():
                    output += channel.recv(4096).decode('utf-8', errors='ignore')
                break
        time.sleep(0.1)
        
    # In shell mode, output might be mixed with bash prompts.
    # Ensure we only return everything. The parsing handles the exact prefix.
    return output

def sync_and_run(host, port, username, password, local_root, remote_root, files_to_sync, run_cmd, screen_session=None):
    start_time = time.time()
    print(f"[{time.time() - start_time:.2f}s] Connecting to {host}:{port} as {username}...")
    try:
        # Create SSH client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Connect with a timeout
        client.connect(host, port=port, username=username, password=password, timeout=10)
        print(f"[{time.time() - start_time:.2f}s] Connected successfully!")
        
        # Open ONE shell channel for everything to avoid massive channel creation delays
        print(f"[{time.time() - start_time:.2f}s] Opening master shell channel...")
        master_channel = client.invoke_shell()
        
        # Sync Files
        print(f"\n[{time.time() - start_time:.2f}s] Preparing sync tasks...")
        
        # Prepare batch script to get abs root and create dirs
        shell_commands = []
        # Turn off bash echo if it's on to avoid confusion
        shell_commands.append("stty -echo 2>/dev/null || true")
        shell_commands.append(f"ABS_ROOT=$(eval echo \"{remote_root}\")")
        shell_commands.append("echo \"ROOT_IS:$ABS_ROOT\"")
        
        # we still need the absolute root locally to compute remote paths
        setup_output = execute_shell_commands(master_channel, shell_commands)
        # print("DEBUG ROOT FETCH:\n", repr(setup_output))
        
        abs_remote_root = ""
        # Sometimes there's carriage returns, so replace \r\n with \n
        setup_output = setup_output.replace("\r", "")
        
        for line in setup_output.splitlines():
            if "ROOT_IS:" in line and "echo" not in line:
                abs_remote_root = line.split("ROOT_IS:")[1].strip()
                break
                
        if not abs_remote_root:
             # Fallback 1: try to find it even with echo
             for line in setup_output.splitlines():
                 if line.startswith("ROOT_IS:"):
                     abs_remote_root = line.split("ROOT_IS:")[1].strip()
                     break
                     
        if not abs_remote_root:
             print(f"Error: Could not resolve remote_root '{remote_root}'. Setup output was: {repr(setup_output)}")
             return
             
        print(f"[{time.time() - start_time:.2f}s] Remote root resolved: {abs_remote_root}")
             
        # Collect all directories to create
        dirs_to_make = {abs_remote_root}
        upload_tasks = []
        
        for rel_path in files_to_sync:
            local_path = os.path.join(local_root, rel_path)
            remote_path = f"{abs_remote_root}/{rel_path}".replace('\\', '/').replace('//', '/')
            if not os.path.exists(local_path):
                print(f"Local file not found, skipping: {local_path}")
                continue
                
            remote_dir = os.path.dirname(remote_path)
            if remote_dir:
                dirs_to_make.add(remote_dir)
            
            upload_tasks.append((local_path, remote_path))
            
        # Create all required directories using fast shell
        if dirs_to_make:
            print(f"[{time.time() - start_time:.2f}s] Creating directories...")
            mkdir_cmd = "mkdir -p " + " ".join(f"\"{d}\"" for d in dirs_to_make)
            execute_shell_commands(master_channel, [mkdir_cmd])

        print(f"[{time.time() - start_time:.2f}s] Uploading files...")
        for local_path, remote_path in upload_tasks:
            print(f"[{time.time() - start_time:.2f}s] Uploading {local_path} -> {remote_path}")
            try:
                # Upload using base64 encoded echo directly in the shell
                with open(local_path, 'rb') as f:
                    content = f.read()
                import base64
                encoded_content = base64.b64encode(content).decode()
                
                # Clear the remote file first
                execute_shell_commands(master_channel, [f"> \"{remote_path}\""])
                
                # Send in chunks of 32KB to avoid ARG_MAX command line length limits
                chunk_size = 32768
                for i in range(0, len(encoded_content), chunk_size):
                    chunk = encoded_content[i:i+chunk_size]
                    cmd = f"echo -n {chunk} | base64 -d >> \"{remote_path}\""
                    execute_shell_commands(master_channel, [cmd])
                    
            except Exception as ex:
                print(f"Failed to upload {local_path} completely: {ex}")
        
        print(f"[{time.time() - start_time:.2f}s] File sync completed.")
        
        # Execute Command
        print(f"\n[{time.time() - start_time:.2f}s] Executing remote command: {run_cmd}")
        
        if screen_session:
            print(f"[{time.time() - start_time:.2f}s] Targeting screen session: {screen_session}")
            
            import uuid
            import base64
            
            log_file = f"/tmp/use_ai_server_{uuid.uuid4().hex}.log"
            script_file = f"/tmp/use_ai_server_script_{uuid.uuid4().hex}.sh"
            end_marker = f"DONE_{uuid.uuid4().hex}"
            
            wrapped_cmd = f"cd \"{abs_remote_root}\" && ({run_cmd}) > {log_file} 2>&1; echo {end_marker} >> {log_file}"
            encoded_script = base64.b64encode(wrapped_cmd.encode()).decode()
            
            # Combine screen check, script creation, and execution into ONE command
            setup_cmd = f"""
            if ! screen -ls | grep -q "{screen_session}"; then
                echo "WARNING_SCREEN_NOT_FOUND"
            fi
            echo {encoded_script} | base64 -d > {script_file}
            chmod +x {script_file}
            screen -S {screen_session} -X stuff 'bash {script_file}'$'\\n'
            """
            
            out = execute_shell_commands(master_channel, [setup_cmd])
            if "WARNING_SCREEN_NOT_FOUND" in out:
                print(f"Warning: Screen session '{screen_session}' not found or not active.")
            
            print(f"[{time.time() - start_time:.2f}s] Command sent to screen. Starting tail...")
            
            # Start tailing the log file using the same master channel
            # To do this in the same channel without blocking, we can send the tail command
            # and read the output continuously until we see the marker.
            tail_cmd = f"touch {log_file} && tail -n +1 -f {log_file}"
            # We don't use execute_shell_commands here because we want to stream the output
            master_channel.send(tail_cmd + "\n")
            
            # Stream output until the end marker is found
            try:
                while True:
                    if master_channel.recv_ready():
                        output = master_channel.recv(4096).decode('utf-8', errors='ignore')
                        
                        # filter out the tail command itself being echoed
                        if tail_cmd in output:
                            output = output.replace(tail_cmd + "\n", "")
                            output = output.replace(tail_cmd + "\r\n", "")
                        
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
                # tail command might still be running in the master_channel, we need to send Ctrl+C
                master_channel.send('\x03') # Ctrl+C
                time.sleep(0.5)
                # clear buffer
                while master_channel.recv_ready():
                    master_channel.recv(4096)
                
                cleanup_cmd = f"rm -f {log_file} {script_file}"
                execute_shell_commands(master_channel, [cleanup_cmd])
            
            print("-" * 40)
            print("\nCommand execution in screen session completed (or log tailing stopped).")
            
        else:
            full_cmd = f"cd \"{abs_remote_root}\" && {run_cmd} 2>&1"
            print(f"Full command: {full_cmd}")
            
            # Exec command using the same channel
            # We can use execute_shell_commands, but we want streaming output
            master_channel.send(full_cmd + "\n")
            
            # We don't have a reliable end marker unless we append one
            import uuid
            end_marker = f"DONE_{uuid.uuid4().hex}"
            master_channel.send(f"echo {end_marker}\n")
            
            # Stream output
            print("-" * 40)
            exit_status = 0
            while True:
                if master_channel.recv_ready():
                    output = master_channel.recv(4096).decode('utf-8', errors='ignore')
                    
                    if end_marker in output:
                        output = output.replace(end_marker, "").strip()
                        if output:
                            sys.stdout.write(output + "\n")
                            sys.stdout.flush()
                        break
                        
                    sys.stdout.write(output)
                    sys.stdout.flush()
                time.sleep(0.1)
                
            print("-" * 40)
            print("\nCommand executed successfully! (Exit code check bypassed in shell mode)")
            
        master_channel.close()
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
