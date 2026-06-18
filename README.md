# DB Data Browser

Lokalna przeglądarka danych PIM dla produktów i systemów budowlanych.

Źródło danych domyślnie:

```text
C:\Users\Admin\Documents\Agent AI do obsługi zapytań budowlanych\dane-z-PIM
```

## Uruchomienie

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

## Zakres

- lista produktów z wyszukiwaniem i filtrem kategorii,
- lista systemów budowlanych z wyszukiwaniem,
- podgląd odkodowanych atrybutów PIM,
- sekcje dla cech własnych, informacji produktowych, opakowań, wariantów, dokumentów i typoszeregu,
- sekcje dla wariantów systemu, warstw i produktów przypisanych do warstw.

## Docker

Zbuduj i uruchom obraz, montując katalog z plikami PIM jako `/data`:

```powershell
docker build -t db-data-browser .
docker run --rm -p 8788:8788 -v "C:\Users\Admin\Documents\Agent AI do obsługi zapytań budowlanych\dane-z-PIM:/data:ro" db-data-browser
```

Na zdalnym komputerze podmień ścieżkę po lewej stronie `-v` na lokalny katalog z plikami:

```text
products.json
productsAttributes.json
productsModels.json
building_elements.json
buildingsElementsAttributes.json
buildingsElementsModels.json
```

Przykład dla Linuksa:

```bash
docker run -d --name db-data-browser \
  -p 8788:8788 \
  -v /opt/pim-data/dane-z-PIM:/data:ro \
  --restart unless-stopped \
  db-data-browser
```

Po starcie otwórz:

```text
http://ADRES_SERWERA:8788
```

Jeśli katalog `dane-z-PIM` leży obok repozytorium, możesz też użyć:

```powershell
docker compose up -d --build
```
