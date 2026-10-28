"""Import linked WAV recordings and ground truth from the BỘ AUDIO worksheet."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
import wave
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS, "p": PACKAGE_NS}
DRIVE_ID_RE = re.compile(r"/d/([^/]+)")


def _column(address: str) -> str:
    return "".join(character for character in address if character.isalpha())


def read_sheet(path: Path, sheet_name: str) -> tuple[list[dict[str, str]], dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        workbook_rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in workbook_rels.findall("p:Relationship", NS)
        }
        sheet = next(
            node
            for node in workbook.findall("m:sheets/m:sheet", NS)
            if node.attrib["name"] == sheet_name
        )
        target = rel_targets[sheet.attrib[f"{{{REL_NS}}}id"]]
        sheet_path = str(PurePosixPath("xl") / target).replace("xl/xl/", "xl/")
        worksheet = ET.fromstring(archive.read(sheet_path))

        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(text.text or "" for text in item.findall(".//m:t", NS)) for item in strings]

        raw_rows: list[dict[str, str]] = []
        for row in worksheet.findall("m:sheetData/m:row", NS):
            values: dict[str, str] = {}
            for cell in row.findall("m:c", NS):
                address = cell.attrib["r"]
                cell_type = cell.attrib.get("t")
                value_node = cell.find("m:v", NS)
                if cell_type == "inlineStr":
                    value = "".join(text.text or "" for text in cell.findall(".//m:t", NS))
                elif value_node is None:
                    value = ""
                elif cell_type == "s":
                    value = shared[int(value_node.text or 0)]
                else:
                    value = value_node.text or ""
                values[_column(address)] = value
            raw_rows.append(values)

        headers = raw_rows[0]
        rows = [
            {headers[column]: value for column, value in raw.items() if column in headers}
            for raw in raw_rows[1:]
        ]

        rel_path = str(
            PurePosixPath(sheet_path).parent
            / "_rels"
            / f"{PurePosixPath(sheet_path).name}.rels"
        )
        hyperlink_targets: dict[str, str] = {}
        if rel_path in archive.namelist():
            relationships = ET.fromstring(archive.read(rel_path))
            hyperlink_targets = {
                rel.attrib["Id"]: rel.attrib.get("Target", "")
                for rel in relationships.findall("p:Relationship", NS)
            }
        links = {
            link.attrib["ref"]: hyperlink_targets.get(link.attrib.get(f"{{{REL_NS}}}id", ""), "")
            for link in worksheet.findall("m:hyperlinks/m:hyperlink", NS)
        }
        return rows, links


def _dialect(region: str) -> str:
    if "Hà Nội" in region:
        return "north"
    if "Đà Nẵng" in region:
        return "central"
    if "Miền Nam" in region or "Vĩnh Long" in region:
        return "south"
    return "unknown"


def _environment(row: dict[str, str]) -> str:
    text = " ".join(
        [
            row.get("LỚP KIỂM", ""),
            row.get("NGƯỜI NÓI / ĐIỀU KIỆN THU", ""),
            row.get("MỤC ĐÍCH KIỂM TRA", ""),
        ]
    ).casefold()
    if "8 khz" in text:
        return "telephone-8khz"
    if "chồng tiếng" in text or "người khác" in text or "echo" in text:
        return "overlapping-speech"
    if "nhiễu" in text or "tiếng xe" in text or "tiếng quạt" in text or "ồn" in text:
        return "noise"
    if "xa mic" in text:
        return "far-mic"
    return "quiet"


def _download(file_id: str, destination: Path) -> None:
    if destination.exists() and destination.stat().st_size > 1024:
        return
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if not data.startswith(b"RIFF") or b"WAVE" not in data[:16]:
        raise ValueError(f"Drive file {file_id} is not a WAV recording")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def import_corpus(
    workbook_path: Path,
    output_root: Path,
    manifest_path: Path,
    source_url: str,
    workers: int,
) -> dict:
    rows, links = read_sheet(workbook_path, "BỘ AUDIO")
    ready: list[tuple[int, dict[str, str], str, str]] = []
    for sheet_row, row in enumerate(rows, start=2):
        status = row.get("TRẠNG THÁI", "")
        reference = row.get("LỜI NÓI THỰC TẾ (sau nghe lại)", "").strip()
        link = links.get(f"H{sheet_row}", "")
        match = DRIVE_ID_RE.search(link)
        if "ĐÃ CÓ WAV" in status and reference and match:
            ready.append((sheet_row, row, link, match.group(1)))

    audio_dir = output_root / "audio" / "sheet_pilot"
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                _download,
                file_id,
                audio_dir / row["MÃ FILE / LINK"],
            ): row["ID"]
            for _, row, _, file_id in ready
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            future.result()
            print(f"downloaded {completed}/{len(futures)}: {futures[future]}", flush=True)

    manifest: list[dict] = []
    for sheet_row, row, link, file_id in ready:
        filename = row["MÃ FILE / LINK"]
        audio_path = audio_dir / filename
        with wave.open(str(audio_path), "rb") as recording:
            sample_rate = recording.getframerate()
            channels = recording.getnchannels()
            sample_width = recording.getsampwidth()
            duration = recording.getnframes() / sample_rate
        speaker_source = row.get("NGƯỜI NÓI / ĐIỀU KIỆN THU", "").split("·")[0].strip()
        speaker_id = f"speaker-{hashlib.sha256(speaker_source.encode()).hexdigest()[:12]}"
        relative_audio = audio_path.relative_to(output_root).as_posix()
        manifest.append(
            {
                "id": row["ID"],
                "audioPath": relative_audio,
                "reference": row["LỜI NÓI THỰC TẾ (sau nghe lại)"].strip(),
                "speakerId": speaker_id,
                "split": "test",
                "dialect": _dialect(row.get("TIỂU VÙNG CỤ THỂ", "")),
                "subregion": row.get("TIỂU VÙNG CỤ THỂ", ""),
                "environment": _environment(row),
                "device": (
                    "laptop"
                    if "mic: laptop" in row.get("NGƯỜI NÓI / ĐIỀU KIỆN THU", "")
                    else "unknown"
                ),
                "durationSeconds": round(duration, 3),
                "sampleRateHz": sample_rate,
                "channels": channels,
                "sampleWidthBytes": sample_width,
                "sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
                "consent": "project-evaluation-source-sheet",
                "consentNote": "Speaker consent not independently verified by the importer",
                "sourceWorkbook": source_url,
                "sourceSheet": "BỘ AUDIO",
                "sourceCell": f"H{sheet_row}",
                "sourceDriveFileId": file_id,
                "sourceDriveUrl": link,
                "prompt": row.get("CÂU CẦN NÓI", ""),
                "testClass": row.get("LỚP KIỂM", ""),
            }
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in manifest),
        encoding="utf-8",
    )
    return {
        "samples": len(manifest),
        "speakers": len({row["speakerId"] for row in manifest}),
        "dialects": {
            dialect: sum(row["dialect"] == dialect for row in manifest)
            for dialect in ("north", "central", "south")
        },
        "durationSeconds": round(sum(row["durationSeconds"] for row in manifest), 3),
        "manifest": str(manifest_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    print(
        json.dumps(
            import_corpus(
                args.workbook,
                args.output_root,
                args.manifest,
                args.source_url,
                args.workers,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
