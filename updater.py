import os
import shutil
import urllib.request
import zipfile
import sys
from io import BytesIO

REPO_OWNER = "katomaro"
REPO_NAME = "katomart"
BRANCHES_TO_TRY = ["master"]

def download_and_extract():
    print(f"Iniciando atualização do {REPO_NAME}...")
    
    zip_data = None
    success_branch = None

    for branch in BRANCHES_TO_TRY:
        url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/heads/{branch}.zip"
        print(f"Tentando baixar de: {url}")
        try:
            with urllib.request.urlopen(url) as response:
                if response.getcode() == 200:
                    zip_data = response.read()
                    success_branch = branch
                    print(f"Download da branch '{branch}' concluído com sucesso.")
                    break
        except Exception as e:
            print(f"Falha ao baixar da branch '{branch}': {e}")

    if not zip_data:
        print("ERRO: Não foi possível baixar a atualização de nenhuma branch.")
        sys.exit(1)

    print("Extraindo arquivos...")
    current_dir = os.getcwd()

    try:
        with zipfile.ZipFile(BytesIO(zip_data)) as zf:
            root_folder = zf.namelist()[0].split('/')[0]

            temp_extract_path = os.path.join(current_dir, "temp_update_extract")
            if os.path.exists(temp_extract_path):
                shutil.rmtree(temp_extract_path)
            os.makedirs(temp_extract_path)
            
            zf.extractall(temp_extract_path)
            
            source_dir = os.path.join(temp_extract_path, root_folder)
            
            print(f"Copiando arquivos de {source_dir} para {current_dir}...")

            for root, dirs, files in os.walk(source_dir):
                relative_path = os.path.relpath(root, source_dir)
                target_path = os.path.join(current_dir, relative_path)
                
                if not os.path.exists(target_path):
                    os.makedirs(target_path)

                for file in files:
                    source_file = os.path.join(root, file)
                    destination_file = os.path.join(target_path, file)
                    if file == os.path.basename(__file__):
                        continue

                    if file in ["settings.json", "_settings.json"]:
                        if os.path.exists(destination_file):
                            print(f"Mantendo configuração atual: {file}")
                            continue
                    try:
                        shutil.copy2(source_file, destination_file)
                    except PermissionError:
                        print(f"AVISO: Não foi possível atualizar '{file}'. O arquivo pode estar em uso.")
                    except Exception as e:
                        print(f"Erro ao copiar '{file}': {e}")

            print("Limpando arquivos temporários...")
            shutil.rmtree(temp_extract_path)
            
    except Exception as e:
        print(f"ERRO CRÍTICO durante a atualização: {e}")
        sys.exit(1)

    print("Atualização de arquivos finalizada com sucesso!")

if __name__ == "__main__":
    download_and_extract()
