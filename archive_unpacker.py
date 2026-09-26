import os
import sys
import re
import shutil
import zipfile
import threading
import queue
import subprocess
import json
import concurrent.futures
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import rarfile
import py7zr

if sys.platform == 'win32':
    import winreg

# --- Константы ---
MAX_WORKERS = 4  # Количество одновременных потоков распаковки
BUFFER_SIZE = 50 * 1024 * 1024  # 50 МБ буфер для копирования файлов


# --- Настройка путей и настроек ---
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()
SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_settings(settings):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --- Настройка UnRAR ---
def _setup_unrar():
    """Ищет unrar и настраивает rarfile с абсолютным путём"""
    possible_names = ['UnRAR.exe', 'unrar.exe', 'unrar']

    # 1. Проверяем временную папку PyInstaller (если программа собрана в один файл)
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            for name in possible_names:
                path = os.path.join(sys._MEIPASS, name)
                if os.path.exists(path):
                    rarfile.UNRAR_TOOL = os.path.abspath(path)
                    print(f"[INFO] UnRAR найден внутри exe: {rarfile.UNRAR_TOOL}")
                    return

        # 2. Проверяем папку, где лежит сам exe
        base_dir = os.path.dirname(sys.executable)
        for name in possible_names:
            path = os.path.join(base_dir, name)
            if os.path.exists(path):
                rarfile.UNRAR_TOOL = os.path.abspath(path)
                print(f"[INFO] UnRAR найден в папке exe: {rarfile.UNRAR_TOOL}")
                return
    else:
        # 3. Если запущено как Python-скрипт
        base_dir = os.path.dirname(os.path.abspath(__file__))
        for name in possible_names:
            path = os.path.join(base_dir, name)
            if os.path.exists(path):
                rarfile.UNRAR_TOOL = os.path.abspath(path)
                print(f"[INFO] UnRAR найден в папке скрипта: {rarfile.UNRAR_TOOL}")
                return

    # 4. Проверяем стандартные папки установки WinRAR (только Windows)
    if sys.platform == 'win32':
        winrar_paths = [
            r"C:\Program Files\WinRAR\UnRAR.exe",
            r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
            os.path.join(os.environ.get('PROGRAMFILES', ''), 'WinRAR', 'UnRAR.exe'),
            os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), 'WinRAR', 'UnRAR.exe')
        ]
        for path in winrar_paths:
            if path and os.path.exists(path):
                rarfile.UNRAR_TOOL = path
                print(f"[INFO] UnRAR найден в папке WinRAR: {rarfile.UNRAR_TOOL}")
                return

    # 5. Ищем в системном PATH
    for name in ['unrar', 'UnRAR']:
        found = shutil.which(name)
        if found:
            rarfile.UNRAR_TOOL = found
            print(f"[INFO] UnRAR найден в PATH: {rarfile.UNRAR_TOOL}")
            return

    print(f"[КРИТИЧЕСКАЯ ОШИБКА] UnRAR не найден!")
    print("Установите WinRAR или положите UnRAR.exe рядом со скриптом.")


_setup_unrar()


class ArchiveUnpackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Умный распаковщик архивов")
        self.root.geometry("750x550")
        self.root.resizable(False, False)

        self._icon_ref = None
        self._current_parts = []
        self._current_icon_path = None

        self.log_queue = queue.Queue()
        self.is_running = False

        self._build_menu()
        self._build_ui()

        # Загрузка иконок
        self._load_default_icon()
        self._load_saved_icon()

        self._handle_command_line_args()

    def _load_default_icon(self):
        """Ищет иконку по умолчанию рядом с программой"""
        default_names = ['icon.ico', 'icon.png', 'icon.gif']
        for name in default_names:
            path = os.path.join(BASE_DIR, name)
            if os.path.exists(path):
                self._apply_icon(path, save=False)
                self._log(f"Загружена иконка по умолчанию: {name}")
                return

    def _load_saved_icon(self):
        """Загружает сохранённую иконку из settings.json"""
        settings = load_settings()
        icon_path = settings.get('icon_path')
        if icon_path and os.path.exists(icon_path):
            self._apply_icon(icon_path, save=False)
            self._log(f"Загружена сохранённая иконка: {os.path.basename(icon_path)}")

    def _apply_icon(self, path, save=True):
        """Применяет иконку к окну, заголовку и панели задач"""
        try:
            abs_path = os.path.abspath(path)

            if sys.platform == 'win32' and abs_path.lower().endswith('.ico'):
                self.root.iconbitmap(default=abs_path)
                self.root.iconbitmap(abs_path)
            else:
                img = tk.PhotoImage(file=abs_path)
                self.root.iconphoto(True, img)
                self._icon_ref = img

            self._current_icon_path = abs_path

            if save:
                save_settings({'icon_path': abs_path})
                self._log(f"Иконка сохранена: {os.path.basename(abs_path)}")
            return True
        except Exception as e:
            self._log(f"Ошибка применения иконки: {e}")
            return False

    def _handle_command_line_args(self):
        """Обработка аргументов командной строки (для запуска из контекстного меню)"""
        if len(sys.argv) > 1:
            arg_path = sys.argv[-1].strip('"')
            if os.path.exists(arg_path):
                self.var_source.set(arg_path)
                self._log(f"Файл из контекстного меню: {arg_path}")

    def _build_menu(self):
        menu_bar = tk.Menu(self.root)
        settings_menu = tk.Menu(menu_bar, tearoff=0)
        settings_menu.add_command(label="Сменить иконку приложения...", command=self._change_window_icon)

        if sys.platform == 'win32':
            settings_menu.add_separator()
            settings_menu.add_command(label="➕ Добавить в контекстное меню Windows", command=self._add_to_context_menu)
            settings_menu.add_command(label="➖ Удалить из контекстного меню Windows",
                                      command=self._remove_from_context_menu)

        menu_bar.add_cascade(label="Настройки", menu=settings_menu)
        self.root.config(menu=menu_bar)

    def _build_ui(self):
        # --- Блок входных данных ---
        frame_in = ttk.LabelFrame(self.root, text="Пути к файлам")
        frame_in.pack(fill="x", padx=10, pady=5)

        # Строка 1: Источник
        frame_source = ttk.Frame(frame_in)
        frame_source.pack(fill="x", padx=5, pady=2)
        ttk.Label(frame_source, text="Источник:", width=10, anchor="w").pack(side="left", padx=5)
        self.var_source = tk.StringVar()
        ttk.Entry(frame_source, textvariable=self.var_source, width=45).pack(side="left", padx=5)
        ttk.Button(frame_source, text="Выбрать", command=self._select_source).pack(side="left", padx=5)

        # Строка 2: Назначение
        frame_dest = ttk.Frame(frame_in)
        frame_dest.pack(fill="x", padx=5, pady=2)
        ttk.Label(frame_dest, text="Куда (опц.):", width=10, anchor="w").pack(side="left", padx=5)
        self.var_dest = tk.StringVar()
        ttk.Entry(frame_dest, textvariable=self.var_dest, width=45).pack(side="left", padx=5)
        ttk.Button(frame_dest, text="Выбрать", command=self._select_dest).pack(side="left", padx=5)

        ttk.Label(frame_in, text="(Если пусто — распакуется в папку архива)", foreground="gray").pack(anchor="w",
                                                                                                      padx=10, pady=2)

        # --- Кнопка старта ---
        self.btn_start = ttk.Button(self.root, text="🚀 НАЧАТЬ РАСПАКОВКУ", command=self._start_process)
        self.btn_start.pack(pady=10)

        # --- Прогресс бар с реальным прогрессом ---
        self.progress = ttk.Progressbar(self.root, mode='determinate', maximum=100)
        self.progress.pack(fill="x", padx=10, pady=5)

        # Метка с процентами
        self.progress_label = ttk.Label(self.root, text="Готово: 0%")
        self.progress_label.pack(pady=2)

        # --- Лог ---
        frame_log = ttk.LabelFrame(self.root, text="Лог операций")
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.text_log = tk.Text(frame_log, height=12, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(frame_log, command=self.text_log.yview)
        self.text_log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.text_log.pack(side="left", fill="both", expand=True)

    def _select_source(self):
        path = filedialog.askopenfilename(title="Выберите файл архива")
        if not path:
            path = filedialog.askdirectory(title="Или выберите папку")
        if path:
            self.var_source.set(path)

    def _select_dest(self):
        path = filedialog.askdirectory(title="Выберите папку для распаковки")
        if path:
            self.var_dest.set(path)

    def _change_window_icon(self):
        path = filedialog.askopenfilename(
            title="Выберите иконку (ico/png)",
            filetypes=[("Иконки и картинки", "*.ico *.png *.gif")]
        )
        if path:
            self._apply_icon(path, save=True)

    def _add_to_context_menu(self):
        if sys.platform != 'win32': return
        try:
            if getattr(sys, 'frozen', False):
                exe_path = sys.executable
                cmd_template = f'"{exe_path}" "%1"'
            else:
                script_path = os.path.abspath(__file__)
                python_exe = sys.executable
                cmd_template = f'"{python_exe}" "{script_path}" "%1"'

            key_name = r"Software\Classes\*\shell\SmartUnpacker"
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_name)
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "Распаковать здесь (Smart Unpacker)")

            # Иконка в контекстном меню
            icon_for_menu = exe_path
            if self._current_icon_path and self._current_icon_path.lower().endswith('.ico'):
                icon_for_menu = self._current_icon_path
            else:
                default_ico = os.path.join(BASE_DIR, 'icon.ico')
                if os.path.exists(default_ico):
                    icon_for_menu = default_ico

            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon_for_menu)

            command_key = winreg.CreateKey(key, "command")
            winreg.SetValueEx(command_key, "", 0, winreg.REG_SZ, cmd_template)
            winreg.CloseKey(command_key)
            winreg.CloseKey(key)
            messagebox.showinfo("Успех", "Утилита добавлена в контекстное меню!")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка реестра:\n{e}")

    def _remove_from_context_menu(self):
        if sys.platform != 'win32': return
        try:
            key_name = r"Software\Classes\*\shell\SmartUnpacker"
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_name + r"\command")
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_name)
            messagebox.showinfo("Успех", "Удалено из контекстного меню.")
        except FileNotFoundError:
            messagebox.showinfo("Инфо", "Утилита и так не была в меню.")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка реестра:\n{e}")

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
        if self.is_running: return
        source = self.var_source.get().strip()
        dest = self.var_dest.get().strip()
        if not source:
            messagebox.showerror("Ошибка", "Укажите исходный файл!")
            return

        self.is_running = True
        self.btn_start.configure(state="disabled")
        self.root.after(100, self._process_logs)
        thread = threading.Thread(target=self._worker, args=(source, dest), daemon=True)
        thread.start()

    def _worker(self, source, dest):
        try:
            if not dest:
                dest = os.path.dirname(source) if os.path.isfile(source) else source
                self._log(f"Распаковка в папку источника: {dest}")

            files_to_process = [source] if os.path.isfile(source) else [os.path.join(source, f) for f in
                                                                        os.listdir(source)]
            archives = self._find_and_group_archives(files_to_process)

            if not archives:
                self._log("Не найдено поддерживаемых архивов.")
                return

            # Подсчитываем общее количество "единиц работы"
            total_work = sum(len(arch['parts']) for arch in archives)
            self._log(f"Найдено архивов: {len(archives)}, общее количество частей: {total_work}")

            # Устанавливаем максимум прогресс-бара
            self.root.after(0, lambda: self.progress.configure(maximum=total_work, value=0))
            self.root.after(0, lambda: self.progress_label.configure(text=f"Готово: 0% (0/{total_work})"))

            completed_work = 0
            lock = threading.Lock()

            def update_progress(work_units):
                nonlocal completed_work
                with lock:
                    completed_work += work_units
                    percent = int((completed_work / total_work) * 100) if total_work > 0 else 100
                    self.root.after(0, lambda: self.progress.configure(value=completed_work))
                    self.root.after(0, lambda: self.progress_label.configure(
                        text=f"Готово: {percent}% ({completed_work}/{total_work})"
                    ))

            # Многопоточная распаковка
            self._log(f"Запуск многопоточной распаковки (потоков: {MAX_WORKERS})...")

            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {}
                for archive_data in archives:
                    future = executor.submit(self._extract_archive, archive_data, dest)
                    futures[future] = archive_data

                for future in concurrent.futures.as_completed(futures):
                    archive_data = futures[future]
                    try:
                        future.result()
                        update_progress(len(archive_data['parts']))
                    except Exception as e:
                        self._log(f"Ошибка при распаковке {os.path.basename(archive_data['first_part'])}: {e}")
                        update_progress(len(archive_data['parts']))

            self._log("=== ВСЕ ОПЕРАЦИИ ЗАВЕРШЕНЫ ===")
            self.root.after(0, lambda: self.progress_label.configure(text="Готово: 100%"))

        except Exception as e:
            self._log(f"КРИТИЧЕСКАЯ ОШИБКА: {str(e)}")
        finally:
            self.is_running = False
            self.root.after(0, lambda: self.btn_start.configure(state="normal"))

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
        """Группирует файлы, определяя первые части разбитых архивов"""
        archives = []
        processed_bases = set()

        for f in files:
            if not os.path.isfile(f): continue
            arc_type = self._read_header(f)
            if not arc_type: continue

            base_name = self._get_base_name(f)
            if base_name in processed_bases: continue
            processed_bases.add(base_name)

            dir_path = os.path.dirname(f)
            parts = []
            for item in os.listdir(dir_path):
                item_path = os.path.join(dir_path, item)
                if os.path.isfile(item_path) and self._get_base_name(item_path) == base_name:
                    item_type = self._read_header(item_path)
                    if item_type == arc_type or arc_type in ['zip', '7z']:
                        parts.append(item_path)

            parts.sort(key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x)])
            archives.append({'type': arc_type, 'parts': parts, 'first_part': parts[0] if parts else f})

        return archives

    def _get_base_name(self, filepath):
        """Умное определение базового имени файла для группировки частей"""
        filename = os.path.basename(filepath)

        patterns = [
            r'^(.+?)\.part\d+\.(rar|zip|7z)$',
            r'^(.+?)\.\d{3}\.(rar|zip|7z)$',
            r'^(.+?)\.7z\.\d{3}$',
            r'^(.+?)\.(rar|zip|7z)$',
            r'^(.+?)\.r\d{2}$',
            r'^(.+?)\.z\d{2}$',
            r'^(.+?)\.s\d{2}$',
            r'^(.+?)\.\d{3}$',
        ]

        for pattern in patterns:
            match = re.match(pattern, filename, re.IGNORECASE)
            if match:
                return match.group(1)

        return os.path.splitext(filename)[0]

    def _extract_archive(self, archive_data, dest_dir):
        arc_type = archive_data['type']
        parts = archive_data['parts']
        first_part = archive_data['first_part']

        self._log(f"Обработка: {os.path.basename(first_part)} (Тип: {arc_type}, Частей: {len(parts)})")
        self._current_parts = parts

        # Временная папка на целевом диске
        temp_dir = os.path.join(dest_dir, f".temp_unpack_{os.getpid()}")
        os.makedirs(temp_dir, exist_ok=True)

        try:
            if arc_type == 'rar':
                self._extract_rar(first_part, dest_dir)
            elif arc_type == '7z':
                if len(parts) > 1:
                    self._log("  -> Сборка разбитого 7Z...")
                    combined_path = os.path.join(temp_dir, "combined_temp.7z")
                    self._combine_files(parts, combined_path)
                    self._extract_7z(combined_path, dest_dir)
                else:
                    self._extract_7z(first_part, dest_dir)
            elif arc_type == 'zip':
                if len(parts) > 1:
                    self._log("  -> Сборка разбитого ZIP...")
                    combined_path = os.path.join(temp_dir, "combined_temp.zip")
                    self._combine_files(parts, combined_path)
                    self._extract_zip(combined_path, dest_dir)
                else:
                    self._extract_zip(first_part, dest_dir)
            return True
        except Exception as e:
            self._log(f"  -> ОШИБКА распаковки: {str(e)}")
            return False
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _combine_files(self, parts, output_path):
        """Сливает части в один файл используя буфер (экономия RAM)"""
        with open(output_path, 'wb') as outf:
            for part in parts:
                with open(part, 'rb') as inf:
                    shutil.copyfileobj(inf, outf, length=BUFFER_SIZE)

    def _extract_rar(self, filepath, dest):
        """Распаковка RAR"""
        unrar_tool = rarfile.UNRAR_TOOL

        # ЖЕСТКАЯ ПРОВЕРКА ПУТИ К UNRAR
        if not unrar_tool or not os.path.isfile(unrar_tool):
            found = shutil.which('unrar') or shutil.which('UnRAR')
            if found:
                unrar_tool = found
                rarfile.UNRAR_TOOL = found
            else:
                self._log(f"  -> ОШИБКА: UnRAR не найден по пути: {unrar_tool}")
                self._log("  -> Установите WinRAR или положите UnRAR.exe рядом со скриптом.")
                return

        self._log(f"  -> Используемый UnRAR: {unrar_tool}")

        if len(self._current_parts) > 1:
            self._log("  -> Разбитый RAR. Запуск...")
            first_part = self._current_parts[0]

            cmd = [unrar_tool, 'x', '-y', '-o+', first_part, dest + os.sep]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=3600,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )

                if result.returncode == 0:
                    self._log("  -> RAR успешно распакован.")
                else:
                    self._log(f"  -> ОШИБКА unrar (код {result.returncode}): {result.stderr.strip()}")
            except Exception as e:
                self._log(f"  -> ОШИБКА вызова unrar: {str(e)}")
        else:
            try:
                with rarfile.RarFile(filepath) as rf:
                    rf.extractall(dest)
                self._log("  -> RAR успешно распакован.")
            except Exception as e:
                self._log(f"  -> ОШИБКА rarfile: {str(e)}")

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