# Cover Fetcher

A desktop application to search for and download album art from various online music services.

<!-- ![Cover Fetcher Screenshot](assets/screenshot.png) -->
<!-- TODO: Add a screenshot, e.g., at ./assets/screenshot.png -->

## Features

*   **Multi-Service Search:** Simultaneously queries multiple online sources.
*   **Image Previews & Viewer:** Visually inspect images before downloading.
*   **Service Management:** Prioritize and toggle art sources via drag-and-drop.
*   **Customizable:** Light/Dark themes, adjustable thumbnail sizes.
*   **Dimension Filtering:** Filter results by minimum image dimensions.
*   **'Current Art' Display:** Show local art for quick comparison (can be auto-set with `--from-file`).
*   **Command-Line Interface**

## Supported Services

*   Bandcamp
*   Discogs
*   iTunes
*   Last.fm
*   MusicBrainz
*   VGMdb

**Note for Discogs:** Using Discogs requires a Personal Access Token. Add this to your `config.json` (see Configuration section) as `discogs_token: "YOUR_TOKEN_HERE"`.

## Installation

???

## Usage

### GUI

```bash
python main.py
```
Enter artist/album, select services, and click "Search".
- Single/double click results to view/save.
- Drag-and-drop services to reorder.
- Scroll result rows horizontally with `Shift + Mouse Wheel`.

### Keyboard Shortcuts

*   `Enter` (in Artist/Album/Min. Dims input fields): Start Search
*   `Alt+D`: Focus Album input field
*   `Ctrl+P`: Open Settings dialog
*   `Ctrl+I`: Set image for 'Current Art' display

### Command-Line

```bash
python main.py [OPTIONS] [query]
```
**Key Options:**
*   `[query]`: Positional argument for quick search: `"Album Title"` or `"Artist - Album"`.
*   `--artist "Name" --album "Title"`: Specify artist and album.
*   `--from-file "/path/to/audio.mp3"`: Extract metadata and search.
*   `--services "itunes,bandcamp"`: Specify active services and their order.
*   `--min-width N --min-height M`: Filter by minimum dimensions.
*   `--no-save-prompt`: Save images directly without a dialog.
*   `--exit-on-download`: Exit after a successful download.

**Example:**
```bash
python main.py --from-file "song.flac" --min-width 600 --no-save-prompt
```
For a full list of CLI options: `python main.py --help`

## Configuration

User preferences (like API keys, theme, default paths) are stored in `config.json`.
*   Location: `~/.config/cover_fetcher/config.json`

Most settings can be configured through the application's Settings dialog (⚙ icon or `Ctrl+P`).