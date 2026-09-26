import os
import sys
import re
import shutil
import zipfile
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import rarfile
import py7zr

# Настройка rarfile для поиска unrar
if getattr(sys, 'frozen', False):
    # Если запущено как exe
    base_path = sys._MEIPASS
    unrar_path = os.path.join(os.path.dirname(sys.executable), 'UnRAR.exe')
    if os.path.exists(unrar_path):
        rarfile.UNRAR_TOOL = unrar_path


class ArchiveUnpackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Умная распаковщик архивов (Split & Normal)")
        self.root.geometry("700x500")
        self.root.resizable(True, True)

        self.log_queue = queue.Queue()
        self.is_running = False

        self._build_ui()

    def _build_ui(self):
        # Поля ввода
        frame_in = ttk.LabelFrame(self.root, text="Входные данные")
        frame_in.pack(fill="x", padx=10, pady=5)

        self.var_source = tk.StringVar()
        ttk.Entry(frame_in, textvariable=self.var_source, width=60).pack(side="left", padx=5, pady=5)
        ttk.Button(frame_in, text="Файл/Папка", command=self._select_source).pack(side="left", padx=5)

        self.var_dest = tk.StringVar()
        ttk.Entry(frame_in, textvariable=self.var_dest, width=60).pack(side="left", padx=5, pady=5)
        ttk.Button(frame_in, text="Куда распаковать", command=self._select_dest).pack(side="left", padx=5)

        # Кнопка старта
        self.btn_start = ttk.Button(self.root, text="НАЧАТЬ РАСПАКОВКУ", command=self._start_process)
        self.btn_start.pack(pady=10)

        # Прогресс бар
        self.progress = ttk.Progressbar(self.root, mode='indeterminate')
        self.progress.pack(fill="x", padx=10, pady=5)

        # Лог
        frame_log = ttk.LabelFrame(self.root, text="Лог операций")
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.text_log = tk.Text(frame_log, height=15, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(frame_log, command=self.text_log.yview)
        self.text_log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.text_log.pack(side="left", fill="both", expand=True)

    def _select_source(self):
        path = filedialog.askopenfilename(title="Выберите любой файл архива (или папку)")
        if not path:
            path = filedialog.askdirectory(title="Или выберите папку с архивами")
        if path:
            self.var_source.set(path)

    def _select_dest(self):
        path = filedialog.askdirectory(title="Выберите папку для распаковки")
        if path:
            self.var_dest.set(path)

    def _log(self, msg):
        self.log_queue.put(msg)

    def _process_logs(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            self.text_log.configure(state="normal")
            self.text_log.insert("end", msg + "\n")
            self.text_log.see("end")
            self.text_log.configure(state="disabled")
        if self.is_running:
            self.root.after(100, self._process_logs)

    def _start_process(self):
        if self.is_running:
            return

        source = self.var_source.get()
        dest = self.var_dest.get()

        if not source or not dest:
            messagebox.showerror("Ошибка", "Укажите источник и папку назначения!")
            return

        self.is_running = True
        self.btn_start.configure(state="disabled")
        self.progress.start()
        self.root.after(100, self._process_logs)

        # Запуск многопоточности
        thread = threading.Thread(target=self._worker, args=(source, dest), daemon=True)
        thread.start()

    def _worker(self, source, dest):
        try:
            files_to_process = []
            if os.path.isfile(source):
                files_to_process.append(source)
            elif os.path.isdir(source):
                for f in os.listdir(source):
                    files_to_process.append(os.path.join(source, f))

            # Фильтруем и группируем файлы
            archives = self._find_and_group_archives(files_to_process)

            if not archives:
                self._log("Не найдено ни одного поддерживаемого архива.")
                return

            for archive_data in archives:
                self._extract_archive(archive_data, dest)

            self._log("=== ВСЕ ОПЕРАЦИИ ЗАВЕРШЕНЫ ===")
        except Exception as e:
            self._log(f"КРИТИЧЕСКАЯ ОШИБКА: {str(e)}")
        finally:
            self.is_running = False
            self.root.after(0, lambda: self.btn_start.configure(state="normal"))
            self.root.after(0, self.progress.stop)

    def _read_header(self, filepath):
        """Читает бинарный заголовок для определения типа архива"""
        try:
            with open(filepath, 'rb') as f:
                header = f.read(8)
            if header.startswith(b'Rar!\x1a\x07'): return 'rar'
            if header.startswith(b'PK\x03\x04') or header.startswith(b'PK\x05\x06') or header.startswith(
                b'PK\x07\x08'): return 'zip'
            if header.startswith(b'7z\xbc\xaf\x27\x1c'): return '7z'
        except Exception:
            pass
        return None

    def _find_and_group_archives(self, files):
        """Группирует файлы, определяя первые части разбитых архивов по заголовку и имени"""
        archives = []
        processed_bases = set()

        for f in files:
            if not os.path.isfile(f): continue

            arc_type = self._read_header(f)
            if not arc_type: continue

            # Пытаемся определить базовое имя для split-архивов
            base_name = self._get_base_name(f)

            if base_name in processed_bases:
                continue
            processed_bases.add(base_name)

            # Ищем все части в этой директории
            dir_path = os.path.dirname(f)
            parts = []
            for item in os.listdir(dir_path):
                item_path = os.path.join(dir_path, item)
                if os.path.isfile(item_path) and self._get_base_name(item_path) == base_name:
                    if self._read_header(item_path) == arc_type or arc_type == 'zip':
                        # Для ZIP части могут не иметь заголовка PK, кроме первой
                        parts.append(item_path)

            # Сортируем части естественно (part1, part2... или 001, 002...)
            parts.sort(key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x)])

            # Первая часть - это та, с которой начинаем
            first_part = parts[0] if parts else f
            archives.append({'type': arc_type, 'parts': parts, 'first_part': first_part})

        return archives

    def _get_base_name(self, filepath):
        """Убирает номера частей из имени файла для группировки"""
        filename = os.path.basename(filepath)
        # Убираем .part1, .001, .r00 и т.д.
        base = re.sub(r'\.(part\d+|\d{3}|r\d{2}|z\d{2})$', '', filename, flags=re.IGNORECASE)
        # Убираем само расширение
        base = os.path.splitext(base)[0]
        return base

    def _extract_archive(self, archive_data, dest_dir):
        arc_type = archive_data['type']
        parts = archive_data['parts']
        first_part = archive_data['first_part']

        self._log(f"Обработка: {os.path.basename(first_part)} (Тип: {arc_type}, Частей: {len(parts)})")

        # Создаем временную папку НА ТОМ ЖЕ ДИСКЕ, куда распаковываем (экономим C:\Temp)
        temp_dir = os.path.join(dest_dir, f".temp_unpack_{os.getpid()}")
        os.makedirs(temp_dir, exist_ok=True)

        try:
            if arc_type == 'rar':
                self._extract_rar(first_part, dest_dir)
            elif arc_type == '7z':
                self._extract_7z(first_part, dest_dir)
            elif arc_type == 'zip':
                if len(parts) > 1:
                    self._log("  -> Сборка разбитого ZIP в временный файл на диске назначения...")
                    combined_path = os.path.join(temp_dir, "combined_temp.zip")
                    self._combine_files(parts, combined_path)
                    self._extract_zip(combined_path, dest_dir)
                else:
                    self._extract_zip(first_part, dest_dir)
        except Exception as e:
            self._log(f"  -> ОШИБКА распаковки: {str(e)}")
        finally:
            # Очищаем временную папку
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _combine_files(self, parts, output_path):
        """Сливает части в один файл используя буфер (экономия RAM)"""
        with open(output_path, 'wb') as outf:
            for part in parts:
                with open(part, 'rb') as inf:
                    shutil.copyfileobj(inf, outf, length=1024 * 1024 * 10)  # Буфер 10 МБ

    def _extract_rar(self, filepath, dest):
        with rarfile.RarFile(filepath) as rf:
            rf.extractall(dest)
        self._log("  -> RAR успешно распакован.")

    def _extract_zip(self, filepath, dest):
        with zipfile.ZipFile(filepath, 'r') as zf:
            zf.extractall(dest)
        self._log("  -> ZIP успешно распакован.")

    def _extract_7z(self, filepath, dest):
        with py7zr.SevenZipFile(filepath, 'r') as z:
            z.extractall(dest)
        self._log("  -> 7Z успешно распакован.")


if __name__ == "__main__":
    root = tk.Tk()
    app = ArchiveUnpackerApp(root)
    root.mainloop()