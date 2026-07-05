import os
import zipfile

def zip_project(src, dest):
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(src):
            # Exclude folders
            for d in ['.venv', '.git', '__pycache__', 'scratch', '.pytest_cache']:
                if d in dirs:
                    dirs.remove(d)
            for file in files:
                if file in ['trader.db', 'trader.zip', 'trader-key.pem', 'zip_project.py']: continue
                if file.endswith('.log'): continue
                file_path = os.path.join(root, file)
                zipf.write(file_path, os.path.relpath(file_path, src))

if __name__ == '__main__':
    zip_project('.', 'trader.zip')
