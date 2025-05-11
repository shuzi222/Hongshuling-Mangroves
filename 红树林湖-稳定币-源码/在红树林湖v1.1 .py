import time
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import threading
import logging
import gc
import psutil
import os
import json
from datetime import datetime
import requests
import sys
from binance.spot import Spot
from binance.lib.utils import config_logging
from binance.error import ClientError, ServerError

# 配置日志，仅保留文件日志输出
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trade_log.txt')
    ]
)

# 密钥文件路径
API_KEYS_FILE = 'api_keys.json'

# 全局API密钥
api_key = None
api_secret = None

# 初始化币安客户端
def initialize_binance():
    global api_key, api_secret
    if not api_key or not api_secret:
        logging.error(f"初始化币安失败: API Key={api_key}, Secret={'set' if api_secret else 'unset'}")
        raise ValueError("API Key或Secret未设置")
    logging.info(f"初始化币安: API Key={api_key[:4]}...{api_key[-4:]}, Secret={'set' if api_secret else 'unset'}")
    return Spot(api_key=api_key, api_secret=api_secret)

# 保存API密钥到文件
def save_api_keys(key, secret):
    try:
        with open(API_KEYS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'api_key': key, 'api_secret': secret}, f, ensure_ascii=False)
        logging.info("API密钥已保存到api_keys.json")
    except Exception as e:
        logging.error(f"保存API密钥失败: {e}")

# 加载API密钥
def load_api_keys():
    if not os.path.exists(API_KEYS_FILE):
        logging.info("未找到api_keys.json，需要输入新密钥")
        return None, None
    try:
        with open(API_KEYS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        key, secret = data.get('api_key'), data.get('api_secret')
        if not key or not secret:
            logging.error("api_keys.json格式错误：缺少api_key或api_secret")
            return None, None
        logging.info(f"已加载API密钥: Key={key[:4]}...{key[-4:]}")
        return key, secret
    except Exception as e:
        logging.error(f"加载API密钥失败: {e}")
        return None, None

# 测试API密钥有效性
def test_api_keys(client):
    for attempt in range(3):
        try:
            response = client.account()
            logging.info(f"API密钥验证成功，账户信息: {response.get('balances')[:2]}...")
            return True
        except ClientError as e:
            if e.error_code == -2014 or "API-key format invalid" in e.error_message:
                logging.error(f"API密钥无效: {e.error_message} (code: {e.error_code})")
                return False
            logging.error(f"客户端错误: {e.error_message} (code: {e.error_code})")
            time.sleep(5)
        except ServerError as e:
            logging.error(f"服务器错误: {e.error_message} (code: {e.status_code})")
            time.sleep(5)
        except Exception as e:
            logging.error(f"验证API密钥失败: {e}")
            time.sleep(5)
    logging.error("API密钥验证失败，多次尝试无果")
    return False

# 测试网络连接
def test_network():
    try:
        response = requests.get("https://api.binance.com/api/v3/ping", timeout=5)
        if response.status_code == 200:
            logging.info("网络连接测试成功: Binance API 可达")
            return True
        else:
            logging.error(f"网络连接测试失败: HTTP {response.status_code}")
            return False
    except requests.RequestException as e:
        logging.error(f"网络连接测试失败: {e}")
        return False

# 验证交易对
def validate_pairs(client, pairs):
    try:
        response = client.exchange_info()
        valid_pairs = []
        for pair in pairs:
            symbol = pair.replace('/', '')
            if any(s['symbol'] == symbol for s in response['symbols']):
                valid_pairs.append(pair)
                logging.info(f"交易对 {pair} 验证通过")
            else:
                logging.warning(f"交易对 {pair} 在币安不可用，已跳过")
        if not valid_pairs:
            logging.warning("无有效交易对，使用默认 BTC/USDT")
            valid_pairs = ['BTC/USDT']
        logging.info(f"有效交易对: {valid_pairs}")
        return valid_pairs
    except Exception as e:
        logging.error(f"验证交易对失败: {e}")
        return ['BTC/USDT']

# 检查交易对支持
def check_pair_support(client):
    try:
        response = client.exchange_info()
        symbols = [s['symbol'] for s in response['symbols']]
        supported = {}
        for pair in ['DAIUSDT', 'FDUSDUSDT', 'USDCUSDT']:
            supported[pair] = pair in symbols
            logging.info(f"交易对 {pair}: {'支持' if pair in symbols else '不支持'}")
        return supported
    except Exception as e:
        logging.error(f"检查交易对支持失败: {e}")
        return {'DAIUSDT': False, 'FDUSDUSDT': False, 'USDCUSDT': False}

# 全局币安客户端
binance = None

# 稳定币列表和交易对
COINS = ['USDT', 'USDC', 'FDUSD', 'DAI']
PAIRS = ['DAI/USDT', 'FDUSD/USDT', 'USDC/USDT']

# 实盘余额
BALANCES = {}

class ApiKeyDialog(tk.Toplevel):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.title("输入API密钥")
        self.geometry("300x200")
        self.callback = callback
        self.transient(parent)
        self.grab_set()

        tk.Label(self, text="API-密钥:").pack(pady=5)
        self.key_entry = tk.Entry(self, width=30)
        self.key_entry.pack(pady=5)

        tk.Label(self, text="密钥:").pack(pady=5)
        self.secret_entry = tk.Entry(self, width=30, show="*")
        self.secret_entry.pack(pady=5)

        tk.Button(self, text="确认", command=self.submit).pack(pady=10)

    def submit(self):
        key = self.key_entry.get().strip()
        secret = self.secret_entry.get().strip()
        if not key or not secret:
            messagebox.showerror("错误", "API-密钥和密钥不能为空！")
            return
        if len(key) < 20 or len(secret) < 20:
            messagebox.showerror("错误", "API-密钥或密钥格式无效，请检查！")
            return
        self.callback(key, secret)
        self.destroy()

class ArbitrageApp:
    def __init__(self, root):
        self.root = root
        self.root.title("树酱量化【红树林型号：稳定币MA30量化v1.0】")
        self.root.geometry("800x600")
        try:
            base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            icon_path = os.path.join(base_path, "jio.ico")
            self.root.iconbitmap(icon_path)
            logging.info("窗口图标设置为 jio.ico")
        except tk.TclError as e:
            logging.error(f"加载窗口图标失败: {e}")
            self.log(f"无法加载 jio.ico，请检查文件是否存在或格式是否正确: {e}")

        self.log_lock = threading.Lock()
        self.backgrounds = ["bg1.jpg", "bg2.jpg", "bg3.jpg"]
        self.current_bg_index = 0

        self.canvas = tk.Canvas(root, width=800, height=600)
        self.canvas.pack(fill="both", expand=True)
        self.load_background(self.backgrounds[self.current_bg_index])

        self.price_label = tk.Label(root, text="实时价格: 等待更新...", font=("Arial", 10), bg="white", justify="left")
        self.price_label.place(x=20, y=60, width=180, height=80)

        self.ma_label = tk.Label(root, text="MA30: 等待更新...", font=("Arial", 10), bg="white", justify="left")
        self.ma_label.place(x=20, y=150, width=180, height=80)

        self.balance_label = tk.Label(root, text="持仓: 等待更新...", font=("Arial", 10), bg="white", justify="left")
        self.balance_label.place(x=20, y=240, width=180, height=80)

        self.status_label = tk.Label(root, text="状态: 初始化中...", font=("Arial", 10), bg="white")
        self.status_label.place(x=20, y=330)

        self.log_text = tk.Text(root, height=7, width=60, font=("Arial", 10))
        self.log_text.place(x=20, y=360)

        self.switch_button = ttk.Button(root, text="切换背景", command=self.switch_background)
        self.switch_button.place(x=20, y=570)

        self.api_button = ttk.Button(root, text="修改密钥", command=self.modify_api_keys)
        self.api_button.place(x=120, y=570)

        self.running = True
        self.network_connected = True
        self.last_trade_time = time.time()
        self.network_failure_count = 0
        self.max_network_failures = 5

        self.wait_for_api_keys()

    def load_background(self, bg_path):
        try:
            base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            bg_full_path = os.path.join(base_path, bg_path)
            image = Image.open(bg_full_path)
            image = image.resize((800, 600), Image.LANCZOS)
            self.bg_image = ImageTk.PhotoImage(image)
            self.canvas.create_image(0, 0, image=self.bg_image, anchor="nw")
            logging.info(f"加载背景图片: {bg_path}")
        except Exception as e:
            logging.error(f"加载背景失败: {e}")
            self.canvas.configure(bg="gray")
            self.log(f"无法加载背景图片 {bg_path}: {e}")

    def switch_background(self):
        try:
            self.current_bg_index = (self.current_bg_index + 1) % len(self.backgrounds)
            self.load_background(self.backgrounds[self.current_bg_index])
        except Exception as e:
            logging.error(f"切换背景失败: {e}")

    def log(self, message):
        with self.log_lock:
            def update_gui():
                try:
                    self.log_text.insert(tk.END, f"{datetime.now()}: {message}\n")
                    self.log_text.see(tk.END)
                except tk.TclError as e:
                    logging.error(f"GUI日志更新失败: {e}")

            try:
                self.root.after(0, update_gui)
                logging.info(message)
            except Exception as e:
                logging.error(f"日志写入失败: {e}")

    def monitor_memory(self):
        process = psutil.Process(os.getpid())
        while self.running:
            try:
                mem_info = process.memory_info()
                mem_usage_mb = mem_info.rss / 1024 / 1024
                self.log(f"内存使用: {mem_usage_mb:.2f} MB")
                gc.collect()
                time.sleep(300)
            except Exception as e:
                self.log(f"内存监控失败: {e}")
                time.sleep(300)

    def initialize_balances(self):
        global BALANCES
        try:
            response = binance.account()
            for coin in COINS:
                for asset in response['balances']:
                    if asset['asset'] == coin:
                        BALANCES[coin] = float(asset['free'])
                        break
                else:
                    BALANCES[coin] = 0.0
            self.log(f"初始余额: {BALANCES}")
            balance_text = "\n".join([f"{coin}: {BALANCES[coin]:.2f}" for coin in COINS])
            self.root.after(0, lambda: self.balance_label.config(text=f"持仓:\n{balance_text}"))
        except ClientError as e:
            if e.error_code == -2014 or "API-key format invalid" in e.error_message:
                self.log(f"初始化余额失败：API密钥无效 - {e.error_message}")
                self.root.after(0, self.modify_api_keys)
            else:
                self.log(f"初始化余额失败：客户端错误 - {e.error_message} (code: {e.error_code})")
        except ServerError as e:
            self.network_connected = False
            self.log(f"初始化余额失败：服务器错误 - {e.error_message} (code: {e.status_code})")
        except Exception as e:
            self.log(f"初始余额失败: {e}")

    def check_network(self):
        global binance
        if not test_network():
            self.network_failure_count += 1
            self.log(f"网络不可达 ({self.network_failure_count}/{self.max_network_failures})")
            if self.network_failure_count >= self.max_network_failures:
                self.log("连续网络失败，请检查本地网络连接！")
                self.network_failure_count = 0
            return False

        try:
            binance.ticker_24hr(symbol=PAIRS[0].replace('/', ''))
            self.network_failure_count = 0
            self.log(f"网络检查成功: {PAIRS[0]}")
            return True
        except ClientError as e:
            self.network_failure_count += 1
            self.log(f"网络错误 ({self.network_failure_count}/{self.max_network_failures}): {e.error_message} (code: {e.error_code})")
            try:
                binance.ticker_24hr(symbol='BTCUSDT')
                self.network_failure_count = 0
                self.log("网络检查成功: BTC/USDT")
                return True
            except ClientError as e2:
                self.log(f"备用交易对(BTC/USDT)错误: {e2.error_message} (code: {e2.error_code})")
            except Exception as e2:
                self.log(f"备用交易对检查失败: {e2}")
            if self.network_failure_count >= self.max_network_failures:
                self.log("连续网络失败，请检查网络或API密钥！尝试重置API连接...")
                try:
                    binance = initialize_binance()
                    if test_api_keys(binance):
                        self.network_failure_count = 0
                        self.log("API连接重置成功")
                        return True
                    else:
                        self.log("重置API后密钥仍无效，请修改密钥")
                        self.root.after(0, self.modify_api_keys)
                except Exception as e:
                    self.log(f"重置API失败: {e}")
            return False
        except ServerError as e:
            self.network_failure_count += 1
            self.log(f"服务器错误 ({self.network_failure_count}/{self.max_network_failures}): {e.error_message} (code: {e.status_code})")
            return False
        except Exception as e:
            self.log(f"网络检查失败: {e}")
            return False

    def get_4h_ma30(self, symbol):
        for attempt in range(3):
            try:
                response = binance.klines(symbol=symbol.replace('/', ''), interval='4h', limit=31)
                closes = [float(candle[4]) for candle in response[:-1]]
                ma30 = np.mean(closes)
                current_price = float(response[-1][4])
                logging.info(f"获取 {symbol} 数据: 价格={current_price:.4f}, MA30={ma30:.4f}")
                return current_price, ma30
            except ClientError as e:
                if e.error_code == -2014 or "API-key format invalid" in e.error_message:
                    self.log(f"获取{symbol} MA30失败：API密钥无效 - {e.error_message}")
                    self.root.after(0, self.modify_api_keys)
                    return None, None
                elif e.error_code == -1121 or "Invalid symbol" in e.error_message:
                    self.log(f"获取{symbol} MA30失败：无效交易对")
                    return None, None
                else:
                    self.log(f"获取{symbol} MA30失败：客户端错误 - {e.error_message} (code: {e.error_code})")
                    time.sleep(2)
            except ServerError as e:
                self.network_connected = False
                self.log(f"获取{symbol} MA30失败：服务器错误 - {e.error_message} (code: {e.status_code})")
                time.sleep(2)
            except Exception as e:
                self.log(f"获取{symbol} MA30失败: {e}")
                time.sleep(2)
        self.log(f"获取{symbol} MA30失败：多次尝试无果")
        return None, None

    def get_all_prices_and_ma(self):
        prices = {}
        ma_values = {}
        for pair in PAIRS:
            price, ma30 = self.get_4h_ma30(pair)
            if price is not None and ma30 is not None:
                prices[pair] = price
                ma_values[pair] = ma30
                self.log(f"成功获取 {pair}: 价格={price:.4f}, MA30={ma30:.4f}")
            else:
                self.log(f"跳过交易对 {pair}：无法获取价格或MA30")
                prices[pair] = None
                ma_values[pair] = None
        return prices, ma_values

    def update_balances(self):
        global BALANCES
        try:
            response = binance.account()
            for coin in COINS:
                for asset in response['balances']:
                    if asset['asset'] == coin:
                        BALANCES[coin] = float(asset['free'])
                        break
                else:
                    BALANCES[coin] = 0.0
            self.log(f"更新余额: {BALANCES}")
            balance_text = "\n".join([f"{coin}: {BALANCES[coin]:.2f}" for coin in COINS])
            self.root.after(0, lambda: self.balance_label.config(text=f"持仓:\n{balance_text}"))
        except ClientError as e:
            if e.error_code == -2014 or "API-key format invalid" in e.error_message:
                self.log(f"更新余额失败：API密钥无效 - {e.error_message}")
                self.root.after(0, self.modify_api_keys)
            else:
                self.log(f"更新余额失败：客户端错误 - {e.error_message} (code: {e.error_code})")
        except ServerError as e:
            self.network_connected = False
            self.log(f"更新余额失败：服务器错误 - {e.error_message} (code: {e.status_code})")
        except Exception as e:
            self.log(f"更新余额失败: {e}")

    def execute_trade(self, from_coin, to_coin, amount, prices, trade_speed):
        global BALANCES
        if from_coin == to_coin:
            return False, f"无效交易: {from_coin} -> {to_coin}"

        amount = BALANCES[from_coin] * trade_speed
        amount = int(amount)
        if amount < 5:
            amount = 5 if BALANCES[from_coin] >= 5 else int(BALANCES[from_coin])
        if amount < 5:
            return False, f"数量不足5枚: {from_coin} (余额: {BALANCES[from_coin]:.2f})"

        try:
            if from_coin != 'USDT' and to_coin == 'USDT':
                pair = f"{from_coin}/USDT"
                if pair not in prices or prices[pair] is None:
                    return False, f"无交易对价格: {pair}"
                params = {
                    'symbol': pair.replace('/', ''),
                    'side': 'SELL',
                    'type': 'MARKET',
                    'quantity': amount
                }
                order = binance.new_order(**params)
                self.log(f"执行卖单: {amount:.0f} {from_coin} -> USDT, 订单ID: {order['orderId']}")
                to_amount = float(order['cummulativeQuoteQty'])
            elif from_coin == 'USDT' and to_coin != 'USDT':
                pair = f"{to_coin}/USDT"
                if pair not in prices or prices[pair] is None:
                    pair = f"USDT/{to_coin}"
                    if pair not in prices or prices[pair] is None:
                        return False, f"无交易对价格: {pair}"
                    to_amount = amount / prices[pair]
                    to_amount = int(to_amount)
                    if to_amount < 5:
                        to_amount = 5 if BALANCES[from_coin] >= 5 * prices[pair] else int(BALANCES[from_coin] / prices[pair])
                    if to_amount < 5:
                        return False, f"目标数量不足5枚: {to_coin} (可得: {to_amount})"
                    params = {
                        'symbol': pair.replace('/', ''),
                        'side': 'BUY',
                        'type': 'MARKET',
                        'quantity': to_amount
                    }
                    order = binance.new_order(**params)
                    self.log(f"执行买单: USDT -> {to_amount:.0f} {to_coin}, 订单ID: {order['orderId']}")
                    amount = float(order['cummulativeQuoteQty'])
            else:
                usdt_pair = f"{from_coin}/USDT"
                target_pair = f"{to_coin}/USDT"
                if usdt_pair not in prices or prices[usdt_pair] is None:
                    return False, f"无交易对价格: {usdt_pair}"
                if target_pair not in prices or prices[target_pair] is None:
                    target_pair = f"USDT/{to_coin}"
                    if target_pair not in prices or prices[target_pair] is None:
                        return False, f"无交易对价格: {target_pair}"
                params = {
                    'symbol': usdt_pair.replace('/', ''),
                    'side': 'SELL',
                    'type': 'MARKET',
                    'quantity': amount
                }
                sell_order = binance.new_order(**params)
                self.log(f"执行卖单: {amount:.0f} {from_coin} -> USDT, 订单ID: {sell_order['orderId']}")
                usdt_amount = float(sell_order['cummulativeQuoteQty'])
                to_amount = usdt_amount / prices[target_pair]
                to_amount = int(to_amount)
                if to_amount < 5:
                    to_amount = 5
                    usdt_amount = to_amount * prices[target_pair]
                    amount = usdt_amount / prices[usdt_pair]
                    amount = int(amount)
                    if amount < 5:
                        amount = 5 if BALANCES[from_coin] >= 5 else int(BALANCES[from_coin])
                    if amount < 5:
                        return False, f"数量不足5枚: {from_coin} (需: {amount})"
                    params = {
                        'symbol': usdt_pair.replace('/', ''),
                        'side': 'SELL',
                        'type': 'MARKET',
                        'quantity': amount
                    }
                    sell_order = binance.new_order(**params)
                    self.log(f"调整卖单: {amount:.0f} {from_coin} -> USDT, 订单ID: {sell_order['orderId']}")
                    usdt_amount = float(sell_order['cummulativeQuoteQty'])
                params = {
                    'symbol': target_pair.replace('/', ''),
                    'side': 'BUY',
                    'type': 'MARKET',
                    'quantity': to_amount
                }
                buy_order = binance.new_order(**params)
                self.log(f"执行买单: USDT -> {to_amount:.0f} {to_coin}, 订单ID: {buy_order['orderId']}")

            self.update_balances()
            speed_text = "50%" if trade_speed == 0.5 else "10%"
            return True, f"交易成功: {amount:.0f} {from_coin} -> {to_amount:.0f} {to_coin} ({speed_text}速度)"
        except ClientError as e:
            if e.error_code == -2014 or "API-key format invalid" in e.error_message:
                self.log(f"交易失败：API密钥无效 - {e.error_message}")
                self.root.after(0, self.modify_api_keys)
                return False, f"交易失败: API密钥无效"
            elif e.error_code == -1013 or "insufficient balance" in e.error_message.lower():
                return False, f"交易失败: {from_coin}余额不足 (需: {amount:.0f})"
            return False, f"交易失败: 客户端错误 - {e.error_message} (code: {e.error_code})"
        except ServerError as e:
            self.network_connected = False
            return False, f"交易失败: 服务器错误 - {e.error_message} (code: {e.status_code})"
        except Exception as e:
            return False, f"交易失败: {e}"

    def update_data(self):
        while self.running:
            try:
                if not self.network_connected:
                    self.root.after(0, lambda: self.status_label.config(text="状态: 网络断开，等待重试..."))
                    self.log("网络断开，暂停运行，1分钟后重试...")
                    time.sleep(60)
                    if self.check_network():
                        self.network_connected = True
                        self.root.after(0, lambda: self.status_label.config(text="状态: 网络恢复，运行中"))
                        self.log("网络恢复，继续运行")
                        self.update_balances()
                    continue

                self.update_balances()
                prices, ma_values = self.get_all_prices_and_ma()

                if not self.network_connected:
                    continue

                def update_gui():
                    try:
                        price_text = "\n".join([f"{pair}: {prices.get(pair, 'N/A'):.4f}" if prices.get(pair) is not None else f"{pair}: N/A" for pair in PAIRS])
                        ma_text = "\n".join([f"{pair}: {ma_values.get(pair, 'N/A'):.4f}" if ma_values.get(pair) is not None else f"{pair}: N/A" for pair in PAIRS])
                        self.price_label.config(text=f"实时价格:\n{price_text}")
                        self.ma_label.config(text=f"MA30:\n{ma_text}")
                        self.status_label.config(text="状态: 运行中")
                        self.log(f"GUI更新: 价格={price_text}, MA30={ma_text}")
                    except tk.TclError as e:
                        logging.error(f"GUI更新失败: {e}")

                self.root.after(0, update_gui)

                current_time = time.time()
                if current_time - self.last_trade_time >= 3600:
                    self.last_trade_time = current_time
                    self.log("开始执行交易逻辑...")

                    stable_pairs = [p for p in PAIRS if p != 'BTC/USDT']
                    if not stable_pairs:
                        self.log("无稳定币交易对，暂停交易逻辑，请检查交易对支持")
                        continue

                    above_ma_coins = []
                    below_ma_coins = []
                    trade_speeds = {}
                    for pair in stable_pairs:
                        price = prices.get(pair)
                        ma30 = ma_values.get(pair)
                        if price and ma30:
                            base_coin = pair.split('/')[0]
                            diff_percent = abs(price - ma30) / ma30
                            trade_speed = 0.5 if diff_percent > 0.0005 else 0.1
                            trade_speeds[base_coin] = trade_speed
                            if price > ma30:
                                above_ma_coins.append(base_coin)
                            elif price < ma30:
                                below_ma_coins.append(base_coin)

                    self.log(f"高于MA30的代币: {above_ma_coins}")
                    self.log(f"低于MA30的代币: {below_ma_coins}")
                    self.log(f"交易速度: {trade_speeds}")

                    for from_coin in above_ma_coins:
                        if from_coin == 'USDT':
                            continue
                        for to_coin in below_ma_coins + ['USDT']:
                            if to_coin == from_coin:
                                continue
                            success, msg = self.execute_trade(from_coin, to_coin, BALANCES[from_coin], prices, trade_speeds[from_coin])
                            self.log(msg)
                            if success:
                                balance_text = "\n".join([f"{coin}: {BALANCES[coin]:.2f}" for coin in COINS])
                                self.root.after(0, lambda: self.balance_label.config(text=f"持仓:\n{balance_text}"))
                            break

                    if 'USDT' not in above_ma_coins:
                        for to_coin in below_ma_coins:
                            if to_coin == 'USDT':
                                continue
                            success, msg = self.execute_trade('USDT', to_coin, BALANCES['USDT'], prices, trade_speeds.get('USDT', 0.01))
                            self.log(msg)
                            if success:
                                balance_text = "\n".join([f"{coin}: {BALANCES[coin]:.2f}" for coin in COINS])
                                self.root.after(0, lambda: self.balance_label.config(text=f"持仓:\n{balance_text}"))

            except Exception as e:
                self.log(f"更新数据失败: {e}")
                self.network_connected = False
                self.root.after(0, lambda: self.status_label.config(text="状态: 网络断开，等待重试..."))

            if self.network_connected:
                time.sleep(5)

    def modify_api_keys(self):
        def update_keys(key, secret):
            global api_key, api_secret, binance
            api_key = key
            api_secret = secret
            logging.info(f"更新API密钥: Key={key[:4]}...{key[-4:]}")
            save_api_keys(key, secret)
            try:
                if not test_network():
                    error_msg = "无法连接到币安API，请检查网络连接（运行 ping api.binance.com）或稍后重试。"
                    self.log(error_msg)
                    messagebox.showerror("错误", error_msg)
                    ApiKeyDialog(self.root, update_keys)
                    return
                binance = initialize_binance()
                if test_api_keys(binance):
                    self.log("API密钥更新成功")
                    global PAIRS
                    supported = check_pair_support(binance)
                    new_pairs = [pair for pair in PAIRS if supported.get(pair.replace('/', ''))]
                    if not new_pairs:
                        new_pairs = ['BTC/USDT']
                        self.log("没找到有效稳定币交易对，使用默认 BTC/USDT")
                    else:
                        self.log(f"更新交易对: {new_pairs}")
                    PAIRS[:] = validate_pairs(binance, new_pairs)
                    self.log(f"有效交易对: {PAIRS}")
                    self.initialize_balances()
                else:
                    error_msg = (
                        "新API密钥无效，请检查：\n"
                        "1. 密钥是否启用（币安官网 > API管理）\n"
                        "2. 密钥是否具有'余额读取'和'现货交易'权限\n"
                        "3. IP是否在白名单（或禁用白名单测试）\n"
                        "4. 网络连接是否稳定（运行 ping api.binance.com）\n"
                        "参考: https://binance-docs.github.io/apidocs/spot/en/"
                    )
                    self.log("新API密钥无效")
                    messagebox.showerror("错误", error_msg)
                    ApiKeyDialog(self.root, update_keys)
            except ClientError as e:
                self.log(f"API密钥更新失败: {e.error_message} (code: {e.error_code})")
                messagebox.showerror("错误", f"API密钥更新失败: {e.error_message}")
                ApiKeyDialog(self.root, update_keys)
            except ServerError as e:
                self.log(f"API密钥更新失败: 服务器错误 - {e.error_message} (code: {e.status_code})")
                messagebox.showerror("错误", f"服务器错误: {e.error_message}")
                ApiKeyDialog(self.root, update_keys)
            except Exception as e:
                self.log(f"API密钥更新失败: {e}")
                messagebox.showerror("错误", f"API密钥更新失败: {e}")
                ApiKeyDialog(self.root, update_keys)

        ApiKeyDialog(self.root, update_keys)

    def wait_for_api_keys(self):
        global api_key, api_secret, binance

        def set_keys(key, secret):
            global api_key, api_secret, binance
            api_key = key
            api_secret = secret
            logging.info(f"设置API密钥: Key={key[:4]}...{key[-4:]}")
            save_api_keys(key, secret)
            try:
                if not test_network():
                    error_msg = "无法连接到币安API，请检查网络连接（运行 ping api.binance.com）或稍后重试。"
                    self.log(error_msg)
                    messagebox.showerror("错误", error_msg)
                    ApiKeyDialog(self.root, set_keys)
                    return

                binance = initialize_binance()
                if test_api_keys(binance):
                    self.log("API密钥验证成功")
                    global PAIRS
                    supported = check_pair_support(binance)
                    new_pairs = [pair for pair in PAIRS if supported.get(pair.replace('/', ''))]
                    if not new_pairs:
                        new_pairs = ['BTC/USDT']
                        self.log("没找到有效稳定币交易对，使用默认 BTC/USDT")
                    else:
                        self.log(f"更新交易对: {new_pairs}")
                    PAIRS[:] = validate_pairs(binance, new_pairs)
                    self.log(f"有效交易对: {PAIRS}")
                    self.initialize_balances()
                    self.update_thread = threading.Thread(target=self.update_data)
                    self.update_thread.daemon = True
                    self.update_thread.start()
                    self.memory_thread = threading.Thread(target=self.monitor_memory)
                    self.memory_thread.daemon = True
                    self.memory_thread.start()
                else:
                    error_msg = (
                        "API密钥验证失败，请检查：\n"
                        "1. 密钥是否启用（币安官网 > API管理）\n"
                        "2. 密钥是否具有'余额读取'和'现货交易'权限\n"
                        "3. IP是否在白名单（或禁用白名单测试）\n"
                        "4. 网络连接是否稳定（运行 ping api.binance.com）\n"
                        "参考: https://binance-docs.github.io/apidocs/spot/en/"
                    )
                    self.log("API密钥验证失败")
                    messagebox.showerror("错误", error_msg)
                    ApiKeyDialog(self.root, set_keys)
            except ClientError as e:
                self.log(f"API密钥初始化失败: {e.error_message} (code: {e.error_code})")
                messagebox.showerror("错误", f"API密钥初始化失败: {e.error_message}")
                ApiKeyDialog(self.root, set_keys)
            except ServerError as e:
                self.log(f"API密钥初始化失败: 服务器错误 - {e.error_message} (code: {e.status_code})")
                messagebox.showerror("错误", f"服务器错误: {e.error_message}")
                ApiKeyDialog(self.root, set_keys)
            except Exception as e:
                self.log(f"API密钥初始化失败: {e}")
                messagebox.showerror("错误", f"API密钥初始化失败: {e}")
                ApiKeyDialog(self.root, set_keys)

        loaded_key, loaded_secret = load_api_keys()
        if loaded_key and loaded_secret:
            api_key = loaded_key
            api_secret = loaded_secret
            try:
                if not test_network():
                    error_msg = "无法连接到币安API，请检查网络连接（运行 ping api.binance.com）或稍后重试。"
                    self.log(error_msg)
                    messagebox.showerror("错误", error_msg)
                    ApiKeyDialog(self.root, set_keys)
                    return
                binance = initialize_binance()
                if test_api_keys(binance):
                    self.log("已加载API密钥并验证成功")
                    global PAIRS
                    supported = check_pair_support(binance)
                    new_pairs = [pair for pair in PAIRS if supported.get(pair.replace('/', ''))]
                    if not new_pairs:
                        new_pairs = ['BTC/USDT']
                        self.log("没找到有效稳定币交易对，使用默认 BTC/USDT")
                    else:
                        self.log(f"更新交易对: {new_pairs}")
                    PAIRS[:] = validate_pairs(binance, new_pairs)
                    self.log(f"有效交易对: {PAIRS}")
                    self.initialize_balances()
                    self.update_thread = threading.Thread(target=self.update_data)
                    self.update_thread.daemon = True
                    self.update_thread.start()
                    self.memory_thread = threading.Thread(target=self.monitor_memory)
                    self.memory_thread.daemon = True
                    self.memory_thread.start()
                else:
                    error_msg = (
                        "已加载的API密钥无效，请检查：\n"
                        "1. 密钥是否启用（币安官网 > API管理）\n"
                        "2. 密钥是否具有'余额读取'和'现货交易'权限\n"
                        "3. IP是否在白名单（或禁用白名单测试）\n"
                        "4. 网络连接是否稳定（运行 ping api.binance.com）\n"
                        "参考: https://binance-docs.github.io/apidocs/spot/en/"
                    )
                    self.log("已加载的API密钥无效")
                    messagebox.showerror("错误", error_msg)
                    ApiKeyDialog(self.root, set_keys)
            except ClientError as e:
                self.log(f"加载API密钥失败: {e.error_message} (code: {e.error_code})")
                messagebox.showerror("错误", f"API密钥加载失败: {e.error_message}")
                ApiKeyDialog(self.root, set_keys)
            except ServerError as e:
                self.log(f"加载API密钥失败: 服务器错误 - {e.error_message} (code: {e.status_code})")
                messagebox.showerror("错误", f"服务器错误: {e.error_message}")
                ApiKeyDialog(self.root, set_keys)
            except Exception as e:
                self.log(f"加载API密钥失败: {e}")
                messagebox.showerror("错误", f"API密钥加载失败: {e}")
                ApiKeyDialog(self.root, set_keys)
        else:
            ApiKeyDialog(self.root, set_keys)

    def on_closing(self):
        self.running = False
        self.root.destroy()

if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = ArbitrageApp(root)
        root.protocol("WM_DELETE_WINDOW", app.on_closing)
        root.mainloop()
    except Exception as e:
        logging.error(f"程序崩溃: {e}", exc_info=True)
        with open('error_log.txt', 'w') as f:
            f.write(str(e))
        messagebox.showerror("错误", f"程序发生错误: {e}")