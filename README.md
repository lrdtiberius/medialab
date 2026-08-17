# MediaLab 0.4.0

**MediaLab** ist ein schlanker, selbst gehosteter Medien-Importer und -Katalog für Docker/Portainer. Der typische Ablauf ist:

`video/New → Kopierabschluss prüfen → TMDb-Erkennung → Umbenennen → NFO/Artwork → Filme, Serien oder Anime-Filme`

Dazu kommen eine schreibgeschützte Mediathek, echte Stream-Analyse mit FFprobe, eine auswählbare Fehlerliste und ein kurzes Diagnose-Log.

## Ordnerregeln

```text
video/
├── _config/
│   └── tmdb.env
├── New/
│   ├── Filme/        optionaler Hinweis
│   ├── Serien/       optionaler Hinweis
│   └── Animes/       optionaler Hinweis
├── Filme/            normale Filme
├── Serien/           alle Serien und Folgen, auch Anime-Serien
└── Animes/           ausschließlich Anime-Filme
```

- normale Filme → `Filme`
- Anime-Filme → `Animes`
- alle Serien und Folgen → `Serien`

## Schutz vor unvollständigen Kopien

MediaLab greift nicht sofort auf neue Dateien zu. Eine Datei in `New` muss mehrere aufeinanderfolgende Scans lang unverändert sein, eine Mindest-Ruhezeit seit der letzten Änderung überschreiten und am Anfang/Ende lesbar sein. Erst danach wird FFprobe/TMDb gestartet.

Standardwerte:

```env
SCAN_INTERVAL_SECONDS=20
FILE_STABLE_SECONDS=120
FILE_STABLE_MIN_CHECKS=3
FILE_STABLE_MTIME_SECONDS=60
```

Bei sehr langsamen NAS-Kopien können `FILE_STABLE_SECONDS` und `FILE_STABLE_MTIME_SECONDS` weiter erhöht werden.

## Technische Analyse mit FFprobe

Erkannt werden unter anderem Auflösung, Video-Codec, HDR, Farbtiefe, Bildrate, Bitrate, Audio-Tracks mit Sprache/Kanälen und interne Untertitelspuren. Die Ergebnisse werden in SQLite zwischengespeichert.

Unter `/library/errors` sind alle fehlgeschlagenen Analysen sichtbar. Einzelne oder mehrere Dateien können ausgewählt und erneut analysiert werden.

## Kurzes Log

Unter `/log` zeigt MediaLab die letzten Logzeilen. Zusätzlich wird `/data/medialab.log` geschrieben und automatisch rotiert (1 MB, zwei Backups).

```env
LOG_LEVEL=INFO
LOG_TAIL_LINES=200
```

## TMDb-Zugangsdaten

Empfohlen:

```text
/volume1/video/_config/tmdb.env
```

```dotenv
TMDB_READ_TOKEN=DEIN_API_READ_ACCESS_TOKEN
TMDB_API_KEY=DEIN_V3_API_KEY
```

Mindestens einer der beiden Werte genügt.

## Portainer-Update von Media Ingest 0.3.1

Das bestehende Datenvolume **nicht löschen**. MediaLab verwendet beim Upgrade automatisch die vorhandene `/data/media-ingest.sqlite3`, falls sie existiert. Dadurch bleiben Verlauf und FFprobe-Cache erhalten.

Image:

```text
medialab:0.4.0
```

Build-Archiv:

```text
medialab-0.4.0-build.tar.gz
```

Das vorhandene externe NAS-Volume `Videos` kann unverändert weiterverwendet werden.

## Branding

Footer:

```text
MediaLab 0.4.0 · by Lrd.Tiberius
```

Support:

```text
https://www.paypal.com/paypalme/SebastianM207
```

## Lizenz

MIT. Drittanbieter-Hinweise siehe `THIRD_PARTY_NOTICES.md`.
