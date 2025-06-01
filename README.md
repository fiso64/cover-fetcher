# Cover Fetcher

A desktop application to search for and download album art from various online music services.

![image](https://github.com/user-attachments/assets/0c8ddf32-52c3-4f02-88b6-bbcd8eb83aec)


## Features

*   **Multi-Service Search:** Simultaneously queries multiple online sources.
*   **Service Management:** Prioritize and toggle art sources via drag-and-drop.
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

**Note for Discogs:** Using Discogs requires a Personal Access Token. Add this to your `config.json` (at `~/.config/cover-fetcher`) as `discogs_token: "YOUR_TOKEN_HERE"`.

## Installation

### Windows

Head to the [Releases page](https://github.com/fiso64/cover-fetcher/releases).

### Linux and macOS

Pre-built applications are not currently available. Please see the "Run from Source" section.

## Usage

### GUI

After installation, run the executable.
Enter artist/album, select services, and click "Search".
* Single/double click results to view/save
* Drag-and-drop services to reorder
* Scroll result rows horizontally with `Shift + Mouse Wheel`
*   `Enter`: Start Search
*   `Alt+D`: Focus Album input field
*   `Ctrl+P`: Open Settings dialog
*   `Ctrl+I`: Set image for 'Current Art' display

### Command-Line

```bash
CoverFetcher.exe [OPTIONS]
```
**Key Options:**
*   `--artist "Name" --album "Title"`: Start a search with provided artist and album.
*   `--from-file "/path/to/audio.mp3"`: Extract metadata and search.
*   `--services "itunes,bandcamp"`: Specify active services.
*   `--min-width N --min-height M`: Filter by minimum dimensions.
*   `--no-save-prompt`: Save images directly without a dialog.
*   `--exit-on-download`: Exit after a successful download.

**Example:**
```bash
CoverFetcher.exe --from-file "song.flac" --min-width 600 --no-save-prompt
```
For a full list of CLI options, run `CoverFetcher.exe --help`.

## Run from Source

1.  **Prerequisites:**
    *   Python

2.  **Clone the Repository:**
    ```bash
    git clone https://github.com/fiso64/cover-fetcher.git
    cd cover-fetcher
    ```

3.  **Create and Activate a Virtual Environment (Recommended):**
    ```bash
    # For Linux/macOS
    python3 -m venv venv
    source venv/bin/activate

    # For Windows (cmd.exe)
    python -m venv venv
    venv\Scripts\activate.bat

    # For Windows (PowerShell)
    python -m venv venv
    .\venv\Scripts\Activate.ps1
    ```

4.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

5.  **Run the Application:**
    ```bash
    python main.py
    ```
