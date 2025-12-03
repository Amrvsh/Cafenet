import os
import sys
import sqlite3
import ctypes
import shutil
import time
import threading
import logging
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk, ImageDraw, ImageFont
from datetime import datetime
import json

USE_TTKBOOTSTRAP = False
try:
    import ttkbootstrap as tb
    from ttkbootstrap import Style
    USE_TTKBOOTSTRAP = True
except Exception:
    USE_TTKBOOTSTRAP = False

APP_TITLE = "Cafenet Pro — Inventory Manager"
CONFIG_FILE = "config.txt"
DB_FILENAME = "inventory.db"
LOG_FILENAME = "actions.log"
ICON_FOLDER_DEFAULT = os.path.join(os.path.expanduser("~"), "Desktop", "Cyber")
BACKUP_FOLDER_NAME = "backups"
DEFAULT_BACKUP_INTERVAL = 60 * 10
LOW_STOCK_THRESHOLD_DEFAULT = 5

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(LOG_FILENAME, encoding="utf-8"),
                              logging.StreamHandler(sys.stdout)])

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def get_connection(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db(db_path):
    conn = get_connection(db_path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 0,
            buy_price INTEGER NOT NULL DEFAULT 0,
            sell_price INTEGER NOT NULL DEFAULT 0,
            sold_qty INTEGER NOT NULL DEFAULT 0
        );
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            action TEXT,
            details TEXT
        );
    """)
    # sales log for history and profit calculations
    c.execute("""
        CREATE TABLE IF NOT EXISTS sales_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            qty INTEGER NOT NULL,
            price_buy INTEGER NOT NULL,
            price_sell INTEGER NOT NULL,
            profit INTEGER NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL
        );
    """)
    # persistent undo stack table
    c.execute("""
        CREATE TABLE IF NOT EXISTS undo_stack (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            payload TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

def log_action(db_path, action, details):
    try:
        conn = get_connection(db_path)
        c = conn.cursor()
        c.execute("INSERT INTO logs (action, details) VALUES (?, ?)", (action, details))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.exception("DB log failed: %s", e)
    logging.info("%s — %s", action, details)

_backup_thread = None
_backup_stop = threading.Event()

def make_backup_periodic(db_path, folder, interval_seconds=DEFAULT_BACKUP_INTERVAL):
    os.makedirs(folder, exist_ok=True)
    def worker():
        while not _backup_stop.is_set():
            try:
                ts = time.strftime("%Y%m%d_%H%M%S")
                dst = os.path.join(folder, f"inventory_backup_{ts}.db")
                shutil.copy2(db_path, dst)
                keep_last_n_backups(folder, 20)
                logging.info("Backup created: %s", dst)
            except Exception as e:
                logging.exception("Backup failed: %s", e)
            _backup_stop.wait(interval_seconds)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t

def keep_last_n_backups(folder, n=20):
    try:
        files = sorted([os.path.join(folder, f) for f in os.listdir(folder) if f.startswith("inventory_backup_")])
        if len(files) > n:
            for f in files[:-n]:
                try:
                    os.remove(f)
                except:
                    pass
    except Exception:
        pass

def stop_backups():
    _backup_stop.set()

_image_cache = {}
def generate_fallback_icon(symbol, bg="#2C2C2C", fg="#FFFFFF", size=(48,48)):
    key = f"fallback_{symbol}_{size}"
    if key in _image_cache:
        return _image_cache[key]
    img = Image.new("RGBA", size, bg)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", int(size[1]*0.5))
    except:
        font = ImageFont.load_default()
    w, h = d.textsize(symbol, font=font)
    d.text(((size[0]-w)/2, (size[1]-h)/2), symbol, font=font, fill=fg)
    tkimg = ImageTk.PhotoImage(img)
    _image_cache[key] = tkimg
    return tkimg

def load_icon(path, size=(48,48), fallback_symbol="?"):
    key = f"{path}_{size}"
    if key in _image_cache:
        return _image_cache[key]
    try:
        if path and os.path.exists(path):
            img = Image.open(path).convert("RGBA")
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS
            img = img.resize(size, resample)
            tkimg = ImageTk.PhotoImage(img)
        else:
            tkimg = generate_fallback_icon(fallback_symbol, size=size)
    except Exception:
        tkimg = generate_fallback_icon(fallback_symbol, size=size)
    _image_cache[key] = tkimg
    return tkimg

def get_database_folder():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = f.read().strip()
                if saved and os.path.exists(saved):
                    return saved
        except:
            pass
    tmp = tk.Toplevel()
    tmp.withdraw()
    folder = filedialog.askdirectory(title="انتخاب محل ذخیره دیتابیس", parent=tmp)
    tmp.destroy()
    if not folder:
        raise SystemExit(0)
    os.makedirs(folder, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(folder)
    return folder

def format_price_display(value):
    try:
        return f"{int(value):,} تومان"
    except:
        return "0 تومان"

def clean_price_text(txt):
    return txt.replace(",", "").replace(" تومان", "").strip()

class CafenetApp(ttk.Frame):
    def __init__(self, root, db_path, icon_folder=None):
        super().__init__(root)
        self.root = root
        self.db_path = db_path
        self.icon_folder = icon_folder or ICON_FOLDER_DEFAULT
        self.backup_interval = DEFAULT_BACKUP_INTERVAL
        self.low_stock_threshold = LOW_STOCK_THRESHOLD_DEFAULT
        self._prev_hover = None
        self._highlight_rows = {}
        self._setup_style()
        self._create_vars()
        self._build_ui()
        init_db(self.db_path)
        self._load_undo_stack()  # load persistent undo entries into memory (for quick display if needed)
        self.refresh_list()
        backups_folder = os.path.join(os.path.dirname(self.db_path), BACKUP_FOLDER_NAME)
        global _backup_thread
        _backup_thread = make_backup_periodic(self.db_path, backups_folder, self.backup_interval)

    def _setup_style(self):
        if USE_TTKBOOTSTRAP:
            self.root_style = Style(theme="darkly")
        else:
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except:
                pass
            style.configure(".", background="#181818", foreground="#EDEDED", font=("Segoe UI", 12))
            style.configure("TLabel", background="#181818", foreground="#EDEDED", font=("Segoe UI", 12))
            style.configure("TButton", padding=6, font=("Segoe UI", 11))

    def _create_vars(self):
        self.var_name = tk.StringVar()
        self.var_qty = tk.StringVar()
        self.var_buy = tk.StringVar()
        self.var_sell = tk.StringVar()
        self.var_search = tk.StringVar()
        # extended filters: همه, کمتر از 5, کمترین موجودی, پرفروش‌ترین, پربازده‌ترین
        self.var_filter = tk.StringVar(value="همه")
        # sell quick quantity
        self.var_sell_qty = tk.IntVar(value=1)

        # in-memory representation of undo stack (mirrors DB table ordering)
        self._undo_stack = []

    def _build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("1024x720")
        self.root.configure(bg="#181818")

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=12, pady=(10,6))
        ttk.Label(top, text=APP_TITLE, font=("Segoe UI", 16, "bold")).pack(side="left")

        content = ttk.Frame(self.root)
        content.pack(fill="both", expand=True, padx=12, pady=8)

        left = ttk.Frame(content)
        left.pack(side="left", fill="y", padx=(0,12))

        ttk.Label(left, text="نام کالا:").pack(anchor="w")
        self.ent_name = tk.Entry(left, textvariable=self.var_name, width=30, bg="#FFFFFF", fg="#000000", insertbackground="#000000")
        self.ent_name.pack(pady=4)

        ttk.Label(left, text="تعداد:").pack(anchor="w")
        self.ent_qty = tk.Entry(left, textvariable=self.var_qty, width=15, bg="#FFFFFF", fg="#000000", insertbackground="#000000", justify="right")
        self.ent_qty.pack(pady=4)

        ttk.Label(left, text="قیمت خرید:").pack(anchor="w")
        self.ent_buy = tk.Entry(left, textvariable=self.var_buy, width=22, bg="#FFFFFF", fg="#000000", insertbackground="#000000", justify="right")
        self.ent_buy.pack(pady=4)
        self.ent_buy.bind("<KeyRelease>", lambda e: self._format_price(self.ent_buy))

        ttk.Label(left, text="قیمت فروش:").pack(anchor="w")
        self.ent_sell = tk.Entry(left, textvariable=self.var_sell, width=22, bg="#FFFFFF", fg="#000000", insertbackground="#000000", justify="right")
        self.ent_sell.pack(pady=4)
        self.ent_sell.bind("<KeyRelease>", lambda e: self._format_price(self.ent_sell))

        btns = ttk.Frame(left)
        btns.pack(pady=10, fill="x")
        ttk.Button(btns, text="افزودن", command=self.add_product).pack(side="left", padx=6)
        # Sell -1 uses sell_item with qty=1
        ttk.Button(btns, text="فروش -1", command=lambda: self.sell_item(1)).pack(side="left", padx=6)
        ttk.Button(btns, text="حذف", command=self.delete_item).pack(side="left", padx=6)
        ttk.Button(btns, text="پاک کردن", command=self.clear_entries).pack(side="left", padx=6)

        # Undo button
        ttk.Button(left, text="Undo آخرین عملیّات", command=self.undo_last).pack(pady=(8,0), fill="x")

        # Quick-sell controls
        quick_frame = ttk.LabelFrame(left, text="فروش سریع")
        quick_frame.pack(fill="x", pady=(8,4))
        ttk.Label(quick_frame, text="تعداد:").pack(side="left", padx=(6,2))
        self.spin_sell_qty = tk.Spinbox(quick_frame, from_=1, to=9999, width=6, textvariable=self.var_sell_qty, justify="right")
        self.spin_sell_qty.pack(side="left", padx=(0,6))
        ttk.Button(quick_frame, text="فروش سریع", command=lambda: self.sell_item(self.var_sell_qty.get() if isinstance(self.var_sell_qty.get(), int) else int(self.var_sell_qty.get()))).pack(side="left", padx=6)

        filter_frame = ttk.Frame(left)
        filter_frame.pack(pady=6, fill="x")
        ttk.Label(filter_frame, text="فیلتر موجودی:").pack(side="left")
        filter_menu = ttk.OptionMenu(filter_frame, self.var_filter, "همه", "همه", "کمتر از 5", "کمترین موجودی", "پرفروش‌ترین", "پربازده‌ترین", command=lambda e: self.refresh_list())
        filter_menu.pack(side="left", padx=4)

        search_frame = ttk.Frame(content)
        search_frame.pack(fill="x", pady=(0,8))
        ttk.Label(search_frame, text="جستجو:").pack(side="left")
        ent_search = tk.Entry(search_frame, textvariable=self.var_search, bg="#FFFFFF", fg="#000000", insertbackground="#000000")
        ent_search.pack(side="left", padx=8, fill="x", expand=True)
        ent_search.bind("<KeyRelease>", lambda e: self.refresh_list())

        # Button to open sales history
        ttk.Button(search_frame, text="تاریخچه فروش", command=self.open_sales_history).pack(side="right", padx=6)

        right = ttk.Frame(content)
        right.pack(side="left", fill="both", expand=True)

        cols = ("ID", "نام", "تعداد", "قیمت خرید", "قیمت فروش", "فروخته شده", "درصد فروش")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", height=18)
        for col in cols:
            self.tree.heading(col, text=col, command=lambda c=col: self.sort_by_column(c, False))
            self.tree.column(col, anchor="center", stretch=True)
        self.tree.pack(fill="both", expand=True, padx=6, pady=(0,8))
        self.tree.tag_configure("odd", background="#1F1F1F")
        self.tree.tag_configure("even", background="#232323")
        self.tree.tag_configure("hover", background="#3A3A3A")
        self.tree.tag_configure("low", background="#555500")
        self.tree.bind("<Motion>", self._on_hover)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Leave>", lambda e: self._clear_hover())

        report = ttk.Frame(right, relief="ridge")
        report.pack(fill="x", padx=6, pady=(0,6))
        self.report_label = ttk.Label(report, text="گزارش خلاصه", justify="left", font=("Segoe UI", 12))
        self.report_label.pack(padx=8, pady=8, anchor="w")

        footer = ttk.Frame(self.root)
        footer.pack(fill="x", padx=12, pady=(6,12))
        self.status_label = ttk.Label(footer, text="آماده", font=("Segoe UI", 11))
        self.status_label.pack(side="left")

    def _format_price(self, entry):
        txt = entry.get()
        cleaned = clean_price_text(txt)
        if cleaned.isdigit():
            entry.delete(0, tk.END)
            entry.insert(0, f"{int(cleaned):,} تومان")

    def db_execute(self, query, params=()):
        conn = get_connection(self.db_path)
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        conn.close()

    def db_query(self, query, params=()):
        conn = get_connection(self.db_path)
        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        return rows

    # --- persistent undo helpers ---
    def _push_undo(self, action, payload):
        """store undo record in DB and in-memory list"""
        try:
            conn = get_connection(self.db_path)
            c = conn.cursor()
            payload_text = json.dumps(payload, ensure_ascii=False)
            c.execute("INSERT INTO undo_stack (action, payload, timestamp) VALUES (?, ?, ?)",
                      (action, payload_text, datetime.now().isoformat()))
            undo_id = c.lastrowid
            conn.commit()
            conn.close()
            # keep in-memory mirror
            self._undo_stack.append({"id": undo_id, "action": action, "payload": payload})
            logging.info("Pushed undo: %s %s", action, payload)
        except Exception as e:
            logging.exception("Failed to push undo: %s", e)

    def _pop_undo_db(self):
        """pop last undo from DB and in-memory; returns dict or None"""
        try:
            conn = get_connection(self.db_path)
            c = conn.cursor()
            c.execute("SELECT id, action, payload FROM undo_stack ORDER BY id DESC LIMIT 1")
            row = c.fetchone()
            if not row:
                conn.close()
                return None
            undo_id, action, payload_text = row
            # delete it
            c.execute("DELETE FROM undo_stack WHERE id=?", (undo_id,))
            conn.commit()
            conn.close()
            try:
                payload = json.loads(payload_text)
            except:
                payload = {}
            # pop in-memory mirror if matches
            if self._undo_stack and self._undo_stack[-1].get("id") == undo_id:
                self._undo_stack.pop()
            else:
                # try to remove by id if present
                for i in range(len(self._undo_stack)-1, -1, -1):
                    if self._undo_stack[i].get("id") == undo_id:
                        self._undo_stack.pop(i)
                        break
            return {"id": undo_id, "action": action, "payload": payload}
        except Exception as e:
            logging.exception("Failed pop undo from DB: %s", e)
            return None

    def _load_undo_stack(self):
        """Load existing undo stack from DB into memory (ordered by id asc)"""
        try:
            rows = self.db_query("SELECT id, action, payload FROM undo_stack ORDER BY id ASC")
            self._undo_stack = []
            for r in rows:
                uid, act, payload_text = r
                try:
                    payload = json.loads(payload_text)
                except:
                    payload = {}
                self._undo_stack.append({"id": uid, "action": act, "payload": payload})
            logging.info("Loaded %d undo entries from DB", len(self._undo_stack))
        except Exception as e:
            logging.exception("Failed to load undo stack: %s", e)
            self._undo_stack = []

    # --- end persistent undo helpers ---

    def _on_hover(self, event):
        row_id = self.tree.identify_row(event.y)
        if self._prev_hover != row_id:
            if self._prev_hover is not None and self.tree.exists(self._prev_hover):
                idx = self.tree.index(self._prev_hover)
                tag = self.tree.item(self._prev_hover, "tags")[0]
                self.tree.item(self._prev_hover, tags=(tag,))
            if row_id:
                self.tree.item(row_id, tags=("hover",))
            self._prev_hover = row_id

    def _clear_hover(self):
        if self._prev_hover and self.tree.exists(self._prev_hover):
            tag = self.tree.item(self._prev_hover, "tags")[0]
            self.tree.item(self._prev_hover, tags=(tag,))
        self._prev_hover = None

    def _on_select(self, event):
        for row in self.tree.get_children():
            tags = self.tree.item(row, "tags")
            if "selected" in tags:
                self.tree.item(row, tags=(tags[0],))
        sel = self.tree.selection()
        for s in sel:
            current_tags = self.tree.item(s, "tags")
            self.tree.item(s, tags=current_tags + ("selected",))

    def refresh_list(self):
        q = self.var_search.get().strip()
        filter_val = self.var_filter.get()

        base_query = "SELECT id, name, qty, buy_price, sell_price, sold_qty FROM products"
        rows = []
        if filter_val in ("پرفروش‌ترین", "پربازده‌ترین"):
            # we'll handle these specially
            if filter_val == "پرفروش‌ترین":
                # order by sold_qty desc
                if q == "":
                    rows = self.db_query(base_query + " ORDER BY sold_qty DESC")
                else:
                    rows = self.db_query(base_query + " WHERE name LIKE ? ORDER BY sold_qty DESC", (f"%{q}%",))
            else:
                # پربازده‌ترین -> order by (sold_qty*(sell-buy)) desc
                if q == "":
                    rows = self.db_query(base_query + " ORDER BY (sold_qty * (sell_price - buy_price)) DESC")
                else:
                    rows = self.db_query(base_query + " WHERE name LIKE ? ORDER BY (sold_qty * (sell_price - buy_price)) DESC", (f"%{q}%",))
        else:
            if q == "":
                rows = self.db_query(base_query + " ORDER BY id DESC")
            else:
                rows = self.db_query(base_query + " WHERE name LIKE ? ORDER BY id DESC", (f"%{q}%",))

        for r in self.tree.get_children():
            self.tree.delete(r)

        # if filter "کمترین موجودی", sort by qty asc
        if filter_val == "کمترین موجودی":
            rows = sorted(rows, key=lambda x: x[2])

        for idx, product in enumerate(rows):
            pid, name, qty, buy, sell, sold = product

            if qty <= 0:
                # keep DB clean: delete items with zero or negative quantity
                try:
                    self.db_execute("DELETE FROM products WHERE id=?", (pid,))
                except:
                    pass
                continue

            if filter_val == "کمتر از 5" and qty >= 5:
                continue

            percent_sold = int(sold / (sold + qty) * 100) if (sold + qty) > 0 else 0
            tag = "odd" if idx % 2 == 0 else "even"

            if qty <= self.low_stock_threshold:
                tag = "low"

            self.tree.insert("", tk.END, values=(pid, name, qty, format_price_display(buy), format_price_display(sell), sold, f"{percent_sold}%"), tags=(tag,))
            self._highlight_rows[pid] = 0

        self.update_report()

    def add_product(self):
        name = self.var_name.get().strip()
        qty = self.var_qty.get().strip()
        buy = clean_price_text(self.var_buy.get())
        sell = clean_price_text(self.var_sell.get())

        if not (name and qty and buy and sell):
            messagebox.showwarning("اطلاعات ناقص", "لطفاً تمام فیلدها را پر کنید.")
            return

        try:
            qi = int(qty)
            bi = int(buy)
            si = int(sell)
        except:
            messagebox.showerror("خطا", "مقادیر عددی معتبر نیستند.")
            return

        # insert and get lastrowid to push undo properly
        try:
            conn = get_connection(self.db_path)
            c = conn.cursor()
            c.execute("INSERT INTO products (name, qty, buy_price, sell_price) VALUES (?, ?, ?, ?)", (name, qi, bi, si))
            last_id = c.lastrowid
            conn.commit()
            conn.close()
        except Exception as e:
            logging.exception("Insert product failed: %s", e)
            messagebox.showerror("خطا", "افزودن کالا با خطا مواجه شد.")
            return

        log_action(self.db_path, "ADD", f"{name} | qty={qi} buy={bi} sell={si}")
        # push undo info (persistent)
        self._push_undo("ADD", {"product_id": last_id})

        self.clear_entries()
        self.refresh_list()
        self.status_label.config(text=f"کالا '{name}' اضافه شد.")

    def clear_entries(self):
        self.var_name.set("")
        self.var_qty.set("")
        self.var_buy.set("")
        self.var_sell.set("")

    def sell_item(self, qty_to_sell=1):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("هیچ کالا انتخاب نشده", "لطفاً یک کالا از لیست انتخاب کنید.")
            return

        try:
            # ensure integer
            if isinstance(qty_to_sell, str):
                qty_to_sell = int(qty_to_sell)
            elif isinstance(qty_to_sell, tk.StringVar):
                qty_to_sell = int(qty_to_sell.get())
            elif isinstance(qty_to_sell, tk.IntVar):
                qty_to_sell = int(qty_to_sell.get())
        except:
            qty_to_sell = 1

        if qty_to_sell < 1:
            qty_to_sell = 1

        values = self.tree.item(sel[0])["values"]
        pid = values[0]

        rows = self.db_query("SELECT name, qty, sold_qty, buy_price, sell_price FROM products WHERE id=?", (pid,))
        if not rows:
            return

        name, qty, sold, buy_price, sell_price = rows[0]

        if qty <= 0:
            messagebox.showinfo("ناموجود", f"کالای '{name}' در انبار موجود نیست.")
            return

        if qty_to_sell > qty:
            if not messagebox.askyesno("عدم موجودی کافی", f"موجودی فعلی {qty} است. آیا مایلید {qty} عدد فروخته شود؟"):
                return
            qty_to_sell = qty

        newq = qty - qty_to_sell
        newsold = sold + qty_to_sell

        # update product quantities
        try:
            conn = get_connection(self.db_path)
            c = conn.cursor()
            c.execute("UPDATE products SET qty=?, sold_qty=? WHERE id=?", (newq, newsold, pid))
            # compute profit and insert sales_log, get its id for undo
            profit = (sell_price - buy_price) * qty_to_sell
            c.execute("INSERT INTO sales_log (product_id, qty, price_buy, price_sell, profit, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                      (pid, qty_to_sell, buy_price, sell_price, profit, datetime.now().isoformat()))
            sales_log_id = c.lastrowid
            conn.commit()
            conn.close()
        except Exception as e:
            logging.exception("Failed to perform sell operation: %s", e)
            messagebox.showerror("خطا", "عملیات فروش با خطا مواجه شد.")
            return

        # push undo info including sales_log_id for reliable undo
        self._push_undo("SELL", {
            "product_id": pid,
            "qty": qty_to_sell,
            "prev_qty": qty,
            "prev_sold": sold,
            "sales_log_id": sales_log_id
        })

        log_action(self.db_path, "SELL", f"{name} id={pid} qty_sold={qty_to_sell} qty_before={qty} qty_after={newq}")

        self.refresh_list()
        self.status_label.config(text=f"{qty_to_sell} عدد از محصول '{name}' فروخته شد.")

    def delete_item(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("هیچ کالا انتخاب نشده", "لطفاً یک کالا از لیست انتخاب کنید.")
            return

        pid = self.tree.item(sel[0])["values"][0]
        rows = self.db_query("SELECT name, qty, buy_price, sell_price, sold_qty FROM products WHERE id=?", (pid,))
        if not rows:
            return
        name, qty, buy, sell, sold = rows[0]

        if not messagebox.askyesno("تأیید حذف", f"آیا از حذف محصول '{name}' اطمینان دارید؟"):
            return

        # store to undo stack (persistent) with full data
        self._push_undo("DELETE", {
            "product_id": pid,
            "data": {"name": name, "qty": qty, "buy": buy, "sell": sell, "sold": sold}
        })

        self.db_execute("DELETE FROM products WHERE id=?", (pid,))
        log_action(self.db_path, "DELETE", f"id={pid} name={name}")
        self.refresh_list()
        self.status_label.config(text=f"محصول '{name}' حذف شد.")

    def undo_last(self):
        entry = self._pop_undo_db()
        if not entry:
            messagebox.showinfo("Undo", "هیچ عملی برای بازگردانی وجود ندارد.")
            return
        act = entry.get("action")
        payload = entry.get("payload", {})
        try:
            if act == "ADD":
                pid = payload.get("product_id")
                # delete product that was added
                self.db_execute("DELETE FROM products WHERE id=?", (pid,))
                log_action(self.db_path, "UNDO_ADD", f"deleted id={pid}")
                self.status_label.config(text="عمل افزودن بازگردانده شد.")
            elif act == "SELL":
                pid = payload.get("product_id")
                qty = payload.get("qty", 1)
                prev_qty = payload.get("prev_qty")
                prev_sold = payload.get("prev_sold")
                sales_log_id = payload.get("sales_log_id")
                # revert product quantities
                if prev_qty is not None and prev_sold is not None:
                    self.db_execute("UPDATE products SET qty=?, sold_qty=? WHERE id=?", (prev_qty, prev_sold, pid))
                else:
                    # best effort: add back qty
                    self.db_execute("UPDATE products SET qty=qty + ?, sold_qty = MAX(sold_qty - ?, 0) WHERE id=?", (qty, qty, pid))
                # remove the sales_log entry if exists (use saved id)
                if sales_log_id:
                    try:
                        self.db_execute("DELETE FROM sales_log WHERE id=?", (sales_log_id,))
                    except:
                        # fallback: remove one matching recent entry
                        try:
                            conn = get_connection(self.db_path)
                            c = conn.cursor()
                            c.execute("SELECT id FROM sales_log WHERE product_id=? AND qty=? ORDER BY timestamp DESC LIMIT 1", (pid, qty))
                            row = c.fetchone()
                            if row:
                                c.execute("DELETE FROM sales_log WHERE id=?", (row[0],))
                                conn.commit()
                            conn.close()
                        except:
                            pass
                log_action(self.db_path, "UNDO_SELL", f"reverted id={pid} qty={qty}")
                self.status_label.config(text="عمل فروش بازگردانده شد.")
            elif act == "DELETE":
                data = payload.get("data", {})
                pid = payload.get("product_id")
                # try to re-insert with same id
                try:
                    conn = get_connection(self.db_path)
                    c = conn.cursor()
                    c.execute("INSERT INTO products (id, name, qty, buy_price, sell_price, sold_qty) VALUES (?, ?, ?, ?, ?, ?)",
                              (pid, data.get("name"), data.get("qty", 0), data.get("buy", 0), data.get("sell", 0), data.get("sold", 0)))
                    conn.commit()
                    conn.close()
                except Exception:
                    # fallback: insert without id
                    try:
                        self.db_execute("INSERT INTO products (name, qty, buy_price, sell_price, sold_qty) VALUES (?, ?, ?, ?, ?)",
                                        (data.get("name"), data.get("qty", 0), data.get("buy", 0), data.get("sell", 0), data.get("sold", 0)))
                    except Exception:
                        logging.exception("Failed restore deleted product.")
                log_action(self.db_path, "UNDO_DELETE", f"restored id={pid} name={data.get('name')}")
                self.status_label.config(text="عمل حذف بازگردانده شد.")
            else:
                messagebox.showwarning("Undo", "نوع عملیات قابل بازگردانی نیست.")
        except Exception as e:
            logging.exception("Undo failed: %s", e)
            messagebox.showerror("خطا", "عمل بازگردانی با خطا مواجه شد.")
        self.refresh_list()

    def update_report(self):
        totals = self.db_query("SELECT COUNT(*), COALESCE(SUM(qty),0), COALESCE(SUM(qty*buy_price),0), COALESCE(SUM(sold_qty),0) FROM products")
        cnt, total_qty, total_value, total_sold = totals[0] if totals else (0, 0, 0, 0)
        # total profit from sales_log
        profit_rows = self.db_query("SELECT COALESCE(SUM(profit),0) FROM sales_log")
        total_profit = profit_rows[0][0] if profit_rows else 0

        txt = f"کالا: {cnt}   موجودی کل: {total_qty}   ارزش موجودی: {total_value:,}   فروخته‌شده: {total_sold}   سود کل: {total_profit:,} تومان"
        self.report_label.config(text=txt)

    def sort_by_column(self, col, reverse):
        data = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        try:
            data.sort(key=lambda t: int(t[0].replace("%","").replace(",","")), reverse=reverse)
        except:
            data.sort(reverse=reverse)

        for index, (val, k) in enumerate(data):
            self.tree.move(k, "", index)

        self.tree.heading(col, command=lambda: self.sort_by_column(col, not reverse))

    def open_sales_history(self):
        # open a Toplevel window showing rows from sales_log
        win = tk.Toplevel(self.root)
        win.title("تاریخچه فروش")
        win.geometry("900x500")
        cols = ("id", "نام کالا", "تعداد", "قیمت خرید", "قیمت فروش", "سود", "زمان")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, anchor="center", stretch=True)
        tree.pack(fill="both", expand=True, padx=6, pady=6)

        # fetch sales with product names
        rows = self.db_query("""
            SELECT s.id, p.name, s.qty, s.price_buy, s.price_sell, s.profit, s.timestamp
            FROM sales_log s
            LEFT JOIN products p ON p.id = s.product_id
            ORDER BY s.timestamp DESC
            LIMIT 2000
        """)
        for r in rows:
            sid, pname, qty, pb, ps, profit, ts = r
            tree.insert("", tk.END, values=(sid, pname or "(نامشخص)", qty, format_price_display(pb), format_price_display(ps), format_price_display(profit), ts))

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=6, pady=6)
        ttk.Button(btn_frame, text="حذف رکورد انتخاب‌شده", command=lambda: self._delete_sales_record(tree)).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="بستن", command=win.destroy).pack(side="right", padx=6)

    def _delete_sales_record(self, treeview):
        sel = treeview.selection()
        if not sel:
            return
        sid = treeview.item(sel[0])["values"][0]
        if messagebox.askyesno("حذف", "آیا از حذف رکورد فروش اطمینان دارید؟"):
            self.db_execute("DELETE FROM sales_log WHERE id=?", (sid,))
            log_action(self.db_path, "DELETE_SALE", f"id={sid}")
            treeview.delete(sel[0])
            self.update_report()
            messagebox.showinfo("حذف", "رکورد حذف شد.")

def main():
    if USE_TTKBOOTSTRAP:
        root = tb.Window(themename="darkly")
    else:
        root = tk.Tk()

    root.withdraw()
    db_folder = get_database_folder()
    db_path = os.path.join(db_folder, DB_FILENAME)

    init_db(db_path)

    root.deiconify()
    app = CafenetApp(root, db_path)
    app.pack(fill="both", expand=True)

    def on_close():
        stop_backups()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

if __name__ == "__main__":
    main()
