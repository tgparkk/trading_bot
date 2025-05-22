import sys 
import subprocess 
import os 
print('Requirements file detected. Installing packages...') 
 
try: 
    with open('requirements.txt', 'r', encoding='utf-8') as f: 
        packages = [line.strip() for line in f if line.strip() and not line.startswith('#')] 
    print(f'Found {len(packages)} packages to install') 
    for pkg in packages: 
        print(f'Installing {pkg}...') 
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--user', pkg]) 
    print('Successfully installed all packages from requirements.txt') 
except Exception as e: 
    print(f'Error reading requirements.txt: {e}') 
    print('Installing essential packages directly...') 
    essential_pkgs = ['python-dotenv', 'requests', 'pandas', 'numpy', 'pyjwt', 'websocket-client', 'websockets', 'PyYAML', 'loguru'] 
    for pkg in essential_pkgs: 
        print(f'Installing {pkg}...') 
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--user', pkg]) 
    print('Successfully installed essential packages') 
