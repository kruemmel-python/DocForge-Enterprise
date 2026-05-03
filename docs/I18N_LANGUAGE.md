# Multilingual WebGUI and LLM Output

DocForge Enterprise supports German and English output control.

## WebGUI

The WebGUI contains a language selector:

```text
DE · Deutsch
EN · English
```

Selecting `EN` switches the visible WebGUI labels to English and submits:

```text
--language en
```

Selecting `DE` submits:

```text
--language de
```

The language choice is stored in browser local storage.

## CLI

German output:

```powershell
docforge-enterprise project.zip --language de --profile balanced
```

English output:

```powershell
docforge-enterprise project.zip --language en --profile balanced
```

## LLM behavior

The selected language is injected into every model stage:

- shard analysis
- file reduction
- module reduction
- single-pass final rendering
- chapter rendering

Code identifiers, file paths, symbols and API names remain unchanged. Natural-language descriptions, risks, notes and final documentation follow the selected language.

## WebGUI example

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860 --root ".docforge_webgui" --max-upload-mb 100
```

Open:

```text
http://127.0.0.1:7860
```

Then choose `EN · English` or `DE · Deutsch`.
