import os
import shutil

SOURCE_DIR = "/extensions"
TARGET_DIR = "/shared/publoader/extensions"

os.makedirs(TARGET_DIR, exist_ok=True)

for item in os.listdir(SOURCE_DIR):
    source_path = os.path.join(SOURCE_DIR, item)
    target_path = os.path.join(TARGET_DIR, item)

    if os.path.isdir(source_path):
        shutil.copytree(source_path, target_path, dirs_exist_ok=True)
    else:
        shutil.copy2(source_path, target_path)

print(f"Extensions synced to {TARGET_DIR}")
