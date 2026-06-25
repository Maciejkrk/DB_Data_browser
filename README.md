# DB Data Browser

Lokalna przeglądarka danych PIM dla produktów i systemów budowlanych.

## Uruchomienie Lokalne

Najprościej: uruchom dwuklikiem:

```text
run_browser.bat
```

Albo z PowerShella:

```powershell
cd "C:\Users\Admin\Documents\Agent AI do obsługi zapytań budowlanych\DB_Data_browser"
.\run_browser.bat
```

Potem otwórz:

```text
http://127.0.0.1:8788
```

Przy pierwszym wejściu aplikacja pokaże ekran importu plików PIM.
Po imporcie możesz wrócić do tego ekranu przyciskiem `Zmień pliki wsadowe`.

## Pliki Do Importu

Wgraj pliki JSON z eksportu PIM. Możesz zaznaczyć wszystkie naraz albo dodawać je po kolei:

```text
productsModels.json
productsAttributes.json
products.json
buildingsElementsModels.json
buildingsElementsAttributes.json
building_elements.json
colors.json
colorParameters.json
colorGroups.json
colorGroupParameters.json
```

Do samego działania przeglądarki produktów i systemów wymagane są pliki podstawowe:

```text
productsAttributes.json
products.json
buildingsElementsAttributes.json
building_elements.json
```

## Zakres

- lista produktów z wyszukiwaniem i filtrem kategorii,
- lista systemów budowlanych z wyszukiwaniem,
- lista kolorów i tekstur z podglądem RGB, miniatur i map tekstur,
- podgląd odkodowanych atrybutów PIM,
- sekcje dla cech własnych, informacji produktowych, opakowań, wariantów, dokumentów i typoszeregu,
- sekcje dla wariantów systemu, warstw i produktów przypisanych do warstw.

## Docker

Na komputerze z Dockerem pobierz repozytorium i uruchom Compose:

```bash
git clone https://github.com/Maciejkrk/DB_Data_browser.git
cd DB_Data_browser
docker compose up -d --build
```

Po starcie otwórz:

```text
http://ADRES_SERWERA:8788
```

Aplikacja pokaże ekran importu. Wgrane pliki zostaną zapisane w wolumenie Dockera `pim-data`, więc zostaną po restarcie kontenera.

Ręczny wariant bez Compose:

```bash
docker build -t db-data-browser .
docker volume create db-data-browser-pim-data
docker run -d --name db-data-browser \
  -p 8788:8788 \
  -v db-data-browser-pim-data:/data \
  --restart unless-stopped \
  db-data-browser
```

Podgląd logów:

```bash
docker logs -f db-data-browser
```

## Review Project Storage

The review project is stored on the server in the same data directory as imported PIM files.
In Docker this directory is the `/data` volume.

Persistent review files:

```text
browser_review_project.json
browser_corrections.json
correction_attachments/
```

Give a client access to the running server URL, for example:

```text
http://SERVER_ADDRESS:8788
```

Their acceptance status, correction notes, resolved flags, and attachments are saved on the server.
Use `Review project` in the application header to set project metadata and export the whole review package.
