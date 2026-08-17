# AI Researcher

Lokální osobní AI Researcher. Každé ráno posbírá novinky z internetu
(RSS/Atom, GitHub releases, arXiv), odfiltruje šum, deduplikuje, ohodnotí
relevanci, uloží historii do SQLite a nechá dva lokální modely (llama.cpp)
napsat ranní briefing v Markdownu: rychlá **Gemma 4 26B-A4B** vytáhne z každého
článku fakta („Story Notes"), pomalejší **Muse Glimmer 30B** nad nimi napíše
analytickou vrstvu. K briefu vzniká i **chat pack** — soubor, který celý
vložíš do ChatGPT/Claude a probereš s ním novinky.

Filozofie: **signal > volume**. „Nic důležitého se nestalo" je validní výsledek.

Local-first: jediné LLM je lokální llama.cpp na `127.0.0.1`. Žádná data se
neposílají do cloudových AI API. Internet se používá pouze ke čtení veřejných
zdrojů.

## Architektura

```text
SOURCES (config/sources.yaml)
   ↓
COLLECT      src/collectors/   rss.py | github.py | arxiv.py | html.py
   ↓          (health tracking: 3× po sobě mrtvý zdroj → varování v briefu)
NORMALIZE    src/processing/normalize.py   (canonical URL, čištění HTML, hash)
   ↓
DEDUPLICATE  src/processing/deduplicate.py (exact URL → canonical URL → title hash → fuzzy title)
   ↓          (duplicity se počítají k originálu: "5 médií píše o tomtéž" = signál důležitosti)
STORE        src/db.py                     (SQLite: data/researcher.db)
   ↓
RANK         src/processing/ranking.py     (score 0–100 bez LLM; + coverage boost, + feedback)
   ↓
LLM RERANK   src/processing/rerank.py      (fast_model, 1 levný call: top 30 → top 10; volitelné)
   ↓
FULLTEXT     src/processing/fulltext.py    (trafilatura stáhne plné texty vybraných článků)
   ↓
STORY NOTES  src/processing/notes.py       (fast_model, 1 call na článek: fakta, čísla, citace,
   ↓                                        „proč může záležet" — vrstva faktů, ~2 min)
ANALYSIS     src/llm/client.py + prompts.py (model = Muse, 1 call nad poznámkami: Executive
   ↓                                        Summary, What Matters Today, Deep Dive, Learn, Watchlist)
BRIEF        output/YYYY-MM-DD-morning-brief.md  (analýza + Story Notes + Sources)
CHAT PACK    output/YYYY-MM-DD-chat-pack.md      (brief + úvodní prompt pro ChatGPT/Claude)
```

Proč dva modely: Muse má nejlepší úsudek, ale na 8 GB GPU jede ~4 tok/s;
Gemma jede ~35 tok/s. Gemma tedy dělá objemnou faktickou práci, Muse dostane
krátký, hutný vstup a píše jen to, kde je jeho kvalita znát. Když Muse selže
(timeout, server), analýzu napíše Gemma — brief dorazí vždy. Soubor briefu se
zapíše už po Story Notes (s poznámkou „analysis in progress") a po doběhnutí
Muse se přepíše finální verzí.

Žádný agent framework, žádný ORM, žádné embeddingy — čistý Python. Dedup je
navržený tak, aby šlo později přidat embedding-based clustering jako další
úroveň za stejným rozhraním (`Deduplicator.check()`).

## Setup (Windows)

Vyžaduje Python 3.12.

```powershell
cd C:\Users\PC\Documents\ai-researcher

# aktivace venv
.\.venv\Scripts\Activate.ps1

# instalace závislostí
pip install -r requirements.txt
```

## Spuštění LLM serveru

llama-server běží v **router módu**: modely jsou popsané v
`config/llama-models.ini` a server je načítá na vyžádání podle pole `model`
v requestu. Na 8 GB GPU je vždy načtený jen jeden (`--models-max 1`),
přepnutí trvá 20–70 s.

```powershell
C:\llama-cuda\bin\llama-server.exe --models-preset config\llama-models.ini --models-max 1 --host 127.0.0.1 --port 8080
```

Server drž na `127.0.0.1` — nevystavuj ho na `0.0.0.0`.

Dostupné modely (RTX 5060 8 GB, Ryzen 5 9600X, 32 GB RAM):

| Název v INI | Model | Rychlost | Použití |
|---|---|---|---|
| `muse-glimmer-30b` | Meta Muse Glimmer 30B, Q4_K_M (16,8 GB) | ~4 tok/s (jen ~40 % vrstev se vejde do VRAM) | analytická vrstva briefu (~8–10 min) |
| `gemma-4-26b-a4b` | Google Gemma 4 26B-A4B MoE, UD-Q4_K_XL (17 GB), experti převážně v RAM (`n-cpu-moe`) | ~35 tok/s | rerank (10 s), Story Notes (~2 min), záložní analýza |

Který model dělá kterou fázi, určuje `config/settings.yaml`
(`llm.model` = analýza, `llm.fast_model` = rerank + Story Notes + záloha);
jednorázově jde analýzu přepnout přepínačem `python -m src.main --model gemma-4-26b-a4b`.
Srovnání obou modelů na stejných článcích (starší jednostupňová syntéza):
`output/2026-08-15-morning-brief.glimmer.md` vs. `output/2026-08-15-morning-brief.gemma.md`.

Glimmer má v chat template výchozí `Reasoning strength: high` — v INI je
nastavené `low` (`chat-template-kwargs`), jinak model propálí většinu
tokenů i času přemýšlením. Stažení modelů:

```powershell
.\.venv\Scripts\hf.exe download unsloth/gemma-4-26B-A4B-it-GGUF gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf
.\.venv\Scripts\hf.exe download meta-models/Muse-Glimmer-30B-GGUF Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf
```

(cesty ke staženým souborům jsou v `config/llama-models.ini`).

## Spuštění Researchera

```powershell
# plný run (sběr + ranking + LLM rerank + syntéza)
python -m src.main

# bez LLM — jen sběr, dedup, ranking a "surový" briefing (na testy)
python -m src.main --no-llm

# týdenní digest (trendy za posledních 7 dní, bez sběru)
python -m src.main --weekly

# další volby
python -m src.main --hours 48            # širší časové okno
python -m src.main --category ai         # jen AI (opakovatelné)
python -m src.main --category markets
python -m src.main --dry-run             # nic nezapisuje, jen spočítá
python -m src.main --top 20              # více položek pro LLM
python -m src.main --model gemma-4-26b-a4b   # syntéza jiným modelem (jen tento run)
```

## Zpětná vazba (učení tvého vkusu)

Články v briefu mají ID — čísla v odkazech `[[123]]`. Ohodnocením učíš
ranker, co tě zajímá:

```powershell
python -m src.feedback 123 --up                  # tohle chci vídat
python -m src.feedback 123 456 --down --note "clickbait"
python -m src.feedback --stats                   # přehled per zdroj
```

Zdroje, které konzistentně hodnotíš kladně, dostávají až +10 ke skóre,
konzistentně záporné až −10.

## Kalendář událostí

`config/calendar.yaml` — ručně udržovaný seznam nadcházejících událostí
(FOMC, CPI, earnings…). Události v horizontu `briefing.calendar_horizon_days`
(default 7 dní) se automaticky objeví ve Watchlistu briefu. Data ber
z oficiálních kalendářů (odkazy v souboru) — nevymýšlet.

## Automatické spuštění po zapnutí PC

Registrace do Windows Task Scheduleru (jednorázově):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

Od té chvíle se **2 minuty po každém přihlášení** spustí
`scripts\run_morning_brief.ps1` (v neděli navíc vygeneruje týdenní digest), který:

1. přeskočí běh, pokud dnešní brief už existuje (druhé přihlášení v týž den
   jen znovu ukáže notifikaci),
2. nastartuje llama-server (router mód, viz výše), pokud neběží, a počká až
   10 minut na jeho health check (pokud nenaběhne, Researcher zapíše fallback
   brief bez LLM),
3. spustí `python -m src.main`,
4. ukáže notifikaci s Executive Summary, a to dvojmo:
   - **Windows toast** (po pár sekundách zmizí do Centra oznámení, Win+N),
   - **trvalé okénko v pravém dolním rohu** (`scripts\show_popup.ps1`) — vždy
     nahoře, zůstane, dokud na něj neklikneš (klik = otevře brief a zavře se;
     křížek = jen zavře),
5. v neděli po dokončení týdenního digestu ukáže druhé okénko (nad prvním)
   s „The Week in Brief",
6. llama-server, který sám nastartoval, zase vypne (uvolní VRAM); ponechat ho
   běžet jde přepínačem `-KeepServer`.

Okénko lze vyzkoušet i samostatně:
`powershell -STA -File scripts\show_popup.ps1 -Title "Test" -Body "Ahoj" -OpenPath output\2026-08-16-morning-brief.md`
(volitelně `-AutoCloseSeconds 5` pro automatické zavření, `-Slot 1` pro
zobrazení nad prvním okénkem, `-NoSound`).

Ruční test úlohy: `Start-ScheduledTask -TaskName 'AI Researcher Morning Brief'`
Log scheduleru: `logs\scheduler.log`
Vynucený nový běh v týž den: `powershell -File scripts\run_morning_brief.ps1 -Force`
Odregistrování: `Unregister-ScheduledTask -TaskName 'AI Researcher Morning Brief' -Confirm:$false`

Celý běh trvá zhruba 12 minut (sběr + rerank + Story Notes na Gemmě ~3 min,
analýza na Muse ~8–10 min) — notifikace tedy přijde chvíli po zapnutí PC; do té
doby GPU a CPU pracují na plný výkon. Soubor se Story Notes je k dispozici už
po ~3 minutách. Chceš-li celý brief do 3 minut, přepni `llm.model` na
`gemma-4-26b-a4b`.

## Kde najdu výsledky

| Co | Kde |
|---|---|
| Ranní briefing | `output/YYYY-MM-DD-morning-brief.md` |
| Chat pack (brief + prompt; celý soubor vlož do ChatGPT/Claude a ptej se) | `output/YYYY-MM-DD-chat-pack.md` |
| Databáze (historie článků, briefingů, learning topics) | `data/researcher.db` |
| Log | `logs/researcher.log` |

Každé tvrzení v briefingu odkazuje na zdrojovou položku (`[[12]](url)`) a na
konci reportu je sekce **Sources** s kompletním mapováním čísel na titulky a URL.

## Konfigurace

Vše je v `config/`:

- **`settings.yaml`** — LLM endpoint/timeouty, který model dělá analýzu a který
  rychlé fáze (`llm.model`, `llm.fast_model`), dedup prahy, kolik položek jde do
  LLM (`ranking.top_items`), minimální skóre, jazyk briefingu (`briefing.language`,
  en/cs), Story Notes a chat pack (`briefing.story_notes`, `briefing.chat_pack`), logging.
- **`llama-models.ini`** — presety pro llama-server (cesty ke GGUF, offload,
  reasoning/thinking přepínače, sampling). Názvy sekcí = názvy modelů.
- **`sources.yaml`** — seznam zdrojů. Nic není natvrdo v kódu.
- **`topics.yaml`** — priority témat: entity, high/medium keywords a negativní
  keywords (penalizace šumu) pro kategorie `ai` a `markets`. Tohle je hlavní
  místo, kde ladíš, co Researcher považuje za důležité.

### Přidání nového RSS zdroje

Do `config/sources.yaml` přidej:

```yaml
  - name: My New Feed
    type: rss
    category: ai          # nebo markets
    url: https://example.com/feed.xml
    enabled: true
    priority: 6           # 0-10, vyšší = důvěryhodnější zdroj
```

### Přidání GitHub repozitáře (releases)

```yaml
  - name: vLLM releases
    type: github
    category: ai
    url: vllm-project/vllm     # "owner/repo" nebo plná github.com URL
    enabled: true
    priority: 5
```

Bez tokenu má GitHub API limit 60 requestů/hod — pro pár repozitářů to bohatě
stačí; překročení se jen zaloguje a run pokračuje.

### Přidání HTML zdroje (weby bez RSS)

```yaml
  - name: Anthropic News
    type: html
    category: ai
    url: https://www.anthropic.com/news
    item_selector: "a[href^='/news/']"   # CSS selektor odkazů na články
    enabled: true
    priority: 10
```

Funguje jen pro server-rendered stránky (bez JavaScriptu). Když zdroj 3×
po sobě nic nevrátí, brief tě upozorní v sekci „Zdroje s problémy".

### Přidání arXiv query

```yaml
  - name: arXiv AI/LLM
    type: arxiv
    category: ai
    url: "http://export.arxiv.org/api/query?search_query=cat:cs.CL&sortBy=submittedDate&sortOrder=descending&max_results=15"
    enabled: true
    priority: 2            # papers schválně nízko; ranking je navíc penalizuje
```

## Testy

```powershell
python -m pytest tests/ -v
```

Testy neběží proti živému internetu ani Glimmeru — feedy jsou inline fixtures,
DB je in-memory.

## Troubleshooting

**„cannot connect to LLM server"** — llama-server neběží. Spusť ho (viz výše),
nebo použij `--no-llm`. Když je server nedostupný, Researcher nespadne — zapíše
fallback briefing (surový výběr top položek) a chybu zaloguje.

**Jeden feed vrací chybu / timeout** — run pokračuje bez něj; chyba je
v `logs/researcher.log`. Nefunkční zdroj vypni `enabled: false`.

**Briefing je prázdný / „nic významného"** — to je validní výsledek, ne bug.
Zkus širší okno `--hours 48`, případně sniž `ranking.min_score` v settings.

**Stejný článek se objevil dvakrát** — zvyš `dedup.fuzzy_title_threshold`
opatrně DOLŮ (např. 85), aby fuzzy match chytal víc variant titulků.

**Druhý run tentýž den vybere „další" články** — články už zařazené do
briefingu mají status `briefed` a znovu se nevybírají; vyberou se další
nejlepší dosud nepoužité. Chceš-li čistý test, smaž `data/researcher.db`.

**GitHub 403** — rate limit bez tokenu (60 req/h). Počkej hodinu, nebo sniž
počet GitHub zdrojů.

**Briefing je uříznutý / „Completion hit max_tokens"** — Glimmer je reasoning
model a část tokenového rozpočtu spotřebuje na přemýšlení. Zvyš
`llm.max_tokens` v settings (a případně `-c` u llama-serveru), nebo sniž
`ranking.top_items`, aby byl prompt kratší. Součet promptu a `max_tokens` se
musí vejít do kontextu serveru.

**LLM timeout** — Glimmer generuje ~4 tok/s. Ověř, že v
`config/llama-models.ini` je `reasoning_strength: low` (s výchozím `high`
model přemýšlí desítky minut). `llm.max_retries` nech na 0 — opakování po
timeoutu jen zahodí půl hodiny práce. Nebo přepni `llm.model` na
`gemma-4-26b-a4b`.

**Glimmer běží pod 1 tok/s a GPU je na 100 %** — v INI je natvrdo
`n-gpu-layers` větší, než se vejde do VRAM; CUDA pak vrstvy odloží do
sdílené systémové paměti přes PCIe. Nech `n-gpu-layers` na auto (`--fit`).

**Model se nenačte / router hlásí neznámý model** — název v `settings.yaml`
musí odpovídat sekci v `config/llama-models.ini` a cesta v `model =` musí
existovat. Seznam a stav modelů: `curl http://127.0.0.1:8080/models`.

## Co záměrně neděláme

Portfolio management, investiční doporučení, e-maily, GUI, embeddingy/RAG,
multi-agent frameworky, placené API. Další přirozený krok:
embedding-based dedup clustering (rozhraní `Deduplicator.check()` je na to
připravené) a automatické ladění vah keywords z nasbíraného feedbacku.
