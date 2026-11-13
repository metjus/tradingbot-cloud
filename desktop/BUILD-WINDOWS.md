# Build Windows aplikácie (EXE + inštalátor)

Build sa robí NA WINDOWS stroji (PyInstaller nevie cross-kompilovať z Linuxu).

## 1. Príprava

```bat
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]" PySide6 pyinstaller
```

## 2. EXE

```bat
.venv\Scripts\pyinstaller --noconfirm --name ARGUS --windowed ^
  --add-data "argus\ui;argus\ui" ^
  --collect-submodules argus ^
  desktop\argus_shell.py
```

Výsledok: `dist\ARGUS\ARGUS.exe` (shell automaticky spustí engine proces
`python -m argus`; pri balení je engine súčasťou toho istého bundle).

## 3. Inštalátor (Inno Setup)

Nainštaluj Inno Setup 6 a skompiluj `desktop/argus.iss`:

```bat
iscc desktop\argus.iss
```

Vznikne `dist\ARGUS-setup.exe`. Inštalátor ponúka voliteľné spúšťanie po
štarte Windows (odkaz v `shell:startup`).

## 4. Dáta a aktualizácie

- Dáta a databáza: `%APPDATA%\ARGUS` (prežívajú aktualizácie aj reinštaláciu)
- Aktualizácia = preinštalovanie novou verziou inštalátora; schéma DB sa
  migruje automaticky pri štarte (schema_meta)
- API kľúče: premenné prostredia alebo `.env` v priečinku aplikácie;
  nikdy sa nesynchronizujú do cloudu
