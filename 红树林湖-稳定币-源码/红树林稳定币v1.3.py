from binance.spot import Spot
import numpy as np
import dearpygui.dearpygui as dpg
import json
import os
import time
import threading
import gc
from datetime import datetime, timedelta
from queue import Queue
import traceback
import logging

# 设置日志
logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s %(message)s')

# API 密钥存储文件
CONFIG_FILE = 'binance_config.json'

# 全局变量
client = None
running = False
lock = threading.Lock()
animation_frame = 0
update_queue = Queue(maxsize=100)  # 限制队列大小
# 所有支持的稳定币和默认交易对
ALL_COINS = ['USDT', 'USDC', 'FDUSD', 'DAI', 'USD1', 'XUSD', 'TUSD', 'USDP']
DEFAULT_PAIRS = ['DAI/USDT', 'FDUSD/USDT', 'USDC/USDT', 'USD1/USDT', 'XUSD/USDT', 'TUSD/USDT', 'USDP/USDT']
selected_pairs = [pair for pair in DEFAULT_PAIRS if pair != 'USD1/USDT']  # 默认排除 USD1/USDT
current_prices = {pair: 0.0 for pair in selected_pairs}
ma_values = {pair: 0.0 for pair in selected_pairs}
price_update_time = None
balances = {coin: 0.0 for coin in ALL_COINS}
last_trade_time = None
trade_speed = 0.1  # 默认10%
ma_threshold = 0.0001  # 默认0.01%
ma_period = 30  # 默认MA30
trade_cooldown = 3600  # 默认1小时（秒）
kline_interval = '4h'  # 默认4小时K线

# 加载或保存 API 密钥
def load_config():
    if os.path.exists(CONFIG_FILE):
        if os.path.getsize(CONFIG_FILE) == 0:
            return {}
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except json.JSONDecodeError:
            return {}
        except Exception:
            return {}
    return {}

def save_config(api_key, api_secret):
    config = {'api_key': api_key, 'api_secret': api_secret}
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        update_queue_put("status_label", "API 密钥已保存")
    except Exception:
        update_queue_put("status_label", "保存 API 密钥失败")

# 初始化 Binance API
def init_binance(api_key, api_secret):
    global client
    try:
        client = Spot(api_key=api_key, api_secret=api_secret, base_url='https://api.binance.com')
        client.time()
        update_queue_put("status_label", "Binance API 初始化成功")
        return True
    except Exception:
        update_queue_put("status_label", "Binance API 初始化失败")
        return False

# 验证交易对
def validate_pairs(pairs):
    try:
        response = client.exchange_info()
        valid_pairs = []
        for pair in pairs:
            symbol = pair.replace('/', '')
            base_coin = pair.split('/')[0]
            if any(s['symbol'] == symbol for s in response['symbols']) and base_coin in ALL_COINS:
                valid_pairs.append(pair)
                update_queue_put("status_label", f"交易对 {pair} 验证通过")
            else:
                update_queue_put("status_label", f"交易对 {pair} 不可用或基础币种 {base_coin} 不支持，已跳过")
                if pair == 'USD1/USDT':
                    update_queue_put("status_label", "USD1/USDT 当前不受 Binance API 支持")
        if not valid_pairs:
            valid_pairs = ['USDC/USDT']
            update_queue_put("status_label", "无有效交易对，使用默认 USDC/USDT")
        return valid_pairs
    except Exception as e:
        update_queue_put("status_label", f"验证交易对失败: {str(e)} - 堆栈: {traceback.format_exc()}")
        return ['USDC/USDT']

# 获取交易对当前价格
def get_pair_price(symbol):
    try:
        ticker = client.ticker_price(symbol=symbol.replace('/', ''))
        return float(ticker['price'])
    except Exception:
        return None

# 获取 K 线数据并计算 MA
def get_klines(symbol, interval='4h', limit=31, ma_period=30):
    try:
        klines = client.klines(symbol=symbol.replace('/', ''), interval=interval, limit=limit)
        # 仅提取收盘价，避免 DataFrame
        closes = np.array([float(k[4]) for k in klines[:-1]], dtype=np.float64)
        # 处理 NaN
        if np.isnan(closes).any():
            closes = np.nan_to_num(closes, nan=closes[~np.isnan(closes)][-1])
        # 计算 MA
        ma = np.mean(closes[-ma_period:]) if len(closes) >= ma_period else None
        current_price = float(klines[-1][4])
        return current_price, ma
    except Exception:
        update_queue_put("status_label", f"获取 {symbol} 数据失败")
        return None, None

# 获取交易对信息
def get_symbol_info(symbol):
    try:
        info = client.get_symbol_info(symbol)
        quantity_precision = info['quantityPrecision']
        min_qty = float(next(filter(lambda x: x['filterType'] == 'LOT_SIZE', info['filters']))['minQty'])
        return quantity_precision, min_qty
    except Exception:
        return 8, 0.0001

# 下单函数
def place_order(symbol, side, quantity):
    try:
        quantity_precision, min_qty = get_symbol_info(symbol.replace('/', ''))
        quantity = round(quantity, quantity_precision)
        if quantity < min_qty:
            update_queue_put("status_label", f"下单失败: 数量 {quantity} 小于最小交易量 {min_qty}")
            return None
        params = {
            'symbol': symbol.replace('/', ''),
            'side': side.upper(),
            'type': 'MARKET',
            'quantity': f"{quantity:.{quantity_precision}f}"
        }
        order = client.new_order(**params)
        return order
    except Exception as e:
        update_queue_put("status_label", f"下单失败: {str(e)}")
        return None

# 更新账户余额
def update_balances():
    global balances
    try:
        account = client.account()
        for coin in ALL_COINS:
            balance = float(next((asset['free'] for asset in account['balances'] if asset['asset'] == coin), 0.0))
            with lock:
                balances[coin] = balance
        update_queue_put("balance_label", "\n".join([f"{coin}: {balances[coin]:.2f}" for coin in ALL_COINS]))
    except Exception:
        update_queue_put("status_label", "更新余额失败")

# 执行交易
def execute_trade(from_coin, to_coin, amount, prices):
    global balances
    if from_coin == to_coin:
        return False, f"无效交易: {from_coin} -> {to_coin}"
    if from_coin not in ALL_COINS or to_coin not in ALL_COINS:
        return False, f"币种不支持: {from_coin} 或 {to_coin} 不在支持列表中"

    amount = balances[from_coin] * trade_speed
    amount = int(amount)
    if amount < 5:
        amount = 5 if balances[from_coin] >= 5 else int(balances[from_coin])
    if amount < 5:
        return False, f"数量不足5枚: {from_coin} (余额: {balances[from_coin]:.2f})"

    try:
        if from_coin != 'USDT' and to_coin == 'USDT':
            pair = f"{from_coin}/USDT"
            if pair not in prices or prices[pair] is None:
                return False, f"无交易对价格: {pair}"
            order = place_order(pair, 'sell', amount)
            if order:
                to_amount = float(order['cummulativeQuoteQty'])
                update_queue_put("status_label", f"卖单成功: {amount:.0f} {from_coin} -> USDT, 订单ID: {order['orderId']}")
                update_balances()
                return True, f"交易成功: {amount:.0f} {from_coin} -> {to_amount:.0f} USDT"
        elif from_coin == 'USDT' and to_coin != 'USDT':
            pair = f"{to_coin}/USDT"
            if pair not in prices or prices[pair] is None:
                return False, f"无交易对价格: {pair}"
            to_amount = amount / prices[pair]
            to_amount = int(to_amount)
            if to_amount < 5:
                to_amount = 5 if balances[from_coin] >= 5 * prices[pair] else int(balances[from_coin] / prices[pair])
            if to_amount < 5:
                return False, f"目标数量不足5枚: {to_coin} (可得: {to_amount})"
            order = place_order(pair, 'buy', to_amount)
            if order:
                update_queue_put("status_label", f"买单成功: USDT -> {to_amount:.0f} {to_coin}, 订单ID: {order['orderId']}")
                update_balances()
                return True, f"交易成功: {amount:.0f} USDT -> {to_amount:.0f} {to_coin}"
        else:
            usdt_pair = f"{from_coin}/USDT"
            target_pair = f"{to_coin}/USDT"
            if usdt_pair not in prices or prices[usdt_pair] is None or target_pair not in prices or prices[target_pair] is None:
                return False, f"无交易对价格: {usdt_pair} 或 {target_pair}"
            sell_order = place_order(usdt_pair, 'sell', amount)
            if not sell_order:
                return False, f"卖单失败: {from_coin} -> USDT"
            usdt_amount = float(sell_order['cummulativeQuoteQty'])
            update_queue_put("status_label", f"卖单成功: {amount:.0f} {from_coin} -> USDT, 订单ID: {sell_order['orderId']}")
            to_amount = usdt_amount / prices[target_pair]
            to_amount = int(to_amount)
            if to_amount < 5:
                to_amount = 5
                usdt_amount = to_amount * prices[target_pair]
                amount = usdt_amount / prices[usdt_pair]
                amount = int(amount)
                if amount < 5:
                    amount = 5 if balances[from_coin] >= 5 else int(balances[from_coin])
                if amount < 5:
                    return False, f"数量不足5枚: {from_coin} (需: {amount})"
                sell_order = place_order(usdt_pair, 'sell', amount)
                if not sell_order:
                    return False, f"调整卖单失败: {from_coin} -> USDT"
                usdt_amount = float(sell_order['cummulativeQuoteQty'])
                update_queue_put("status_label", f"调整卖单成功: {amount:.0f} {from_coin} -> USDT, 订单ID: {sell_order['orderId']}")
            buy_order = place_order(target_pair, 'buy', to_amount)
            if buy_order:
                update_queue_put("status_label", f"买单成功: USDT -> {to_amount:.0f} {to_coin}, 订单ID: {buy_order['orderId']}")
                update_balances()
                return True, f"交易成功: {amount:.0f} {from_coin} -> {to_amount:.0f} {to_coin}"
        return False, "交易失败"
    except Exception as e:
        return False, f"交易失败: {str(e)} - 堆栈: {traceback.format_exc()}"

# 主交易循环
def trading_loop():
    global running, animation_frame, last_trade_time
    interval_seconds = 5

    while running:
        try:
            # 更新价格和MA
            for pair in selected_pairs:
                base_coin = pair.split('/')[0]
                if base_coin not in ALL_COINS:
                    update_queue_put("status_label", f"跳过 {pair}：基础币种 {base_coin} 不在支持列表")
                    continue
                price, ma = get_klines(pair, interval=kline_interval, ma_period=ma_period)
                if price and ma:
                    with lock:
                        current_prices[pair] = price
                        ma_values[pair] = ma
                    update_queue_put("price_label", "\n".join([f"{pair}: {current_prices[pair]:.4f}" for pair in selected_pairs]))
                    update_queue_put("ma_label", "\n".join([f"{pair}: {ma_values[pair]:.4f}" for pair in selected_pairs]))
                else:
                    update_queue_put("status_label", f"获取 {pair} 数据失败")

            # 更新余额
            update_balances()

            # 更新GUI状态
            animation_frame += 1
            color = (255, 255, 0) if animation_frame % 20 < 10 else (0, 255, 255)
            update_queue_put("price_label", None, color)
            update_queue_put("ma_label", None, color)
            t_status = (animation_frame % 40) / 40.0
            r_status = int(255 * t_status)
            g_status = int(255 * (1 - t_status))
            b_status = 255
            update_queue_put("status_label", None, (r_status, g_status, b_status))

            # 交易逻辑
            now = datetime.now()
            if last_trade_time is None or (now - last_trade_time).total_seconds() >= trade_cooldown:
                last_trade_time = now
                above_ma_coins = []
                below_ma_coins = []
                trade_speeds = {}
                for pair in selected_pairs:
                    base_coin = pair.split('/')[0]
                    if base_coin not in ALL_COINS:
                        update_queue_put("status_label", f"跳过 {pair}：基础币种 {base_coin} 不在支持列表")
                        continue
                    price = current_prices.get(pair)
                    ma = ma_values.get(pair)
                    if price and ma:
                        diff_percent = abs(price - ma) / ma
                        if diff_percent > ma_threshold:
                            trade_speeds[base_coin] = 0.5 if diff_percent > 0.0005 else trade_speed
                            if price > ma:
                                above_ma_coins.append(base_coin)
                            elif price < ma:
                                below_ma_coins.append(base_coin)
                        else:
                            update_queue_put("status_label", f"{pair} 偏离MA不足 {ma_threshold * 100:.2f}%，跳过")
                update_queue_put("status_label", f"高于MA: {above_ma_coins}, 低于MA: {below_ma_coins}")
                for from_coin in above_ma_coins:
                    if from_coin == 'USDT':
                        continue
                    for to_coin in below_ma_coins + ['USDT']:
                        if to_coin == from_coin:
                            continue
                        success, msg = execute_trade(from_coin, to_coin, balances[from_coin], current_prices)
                        update_queue_put("status_label", msg)
                        if success:
                            update_balances()
                        break
                if 'USDT' not in above_ma_coins:
                    for to_coin in below_ma_coins:
                        if to_coin == 'USDT':
                            continue
                        success, msg = execute_trade('USDT', to_coin, balances['USDT'], current_prices)
                        update_queue_put("status_label", msg)
                        if success:
                            update_balances()
            gc.collect()
        except Exception as e:
            update_queue_put("status_label", f"交易循环错误: {str(e)} - 堆栈: {traceback.format_exc()}")
        time.sleep(interval_seconds)

# 界面更新回调
last_update = 0
def update_ui_callback():
    global last_update
    now = time.time()
    if now - last_update < 0.1:  # 100ms 节流
        return
    last_update = now
    while not update_queue.empty():
        try:
            item = update_queue.get_nowait()
            tag = item[0]
            value = item[1]
            color = item[2] if len(item) > 2 else None
            try:
                if value is not None:
                    dpg.set_value(tag, value)
                if color is not None:
                    dpg.configure_item(tag, color=color)
            except Exception:
                pass
        except Queue.Empty:
            break

# 辅助函数：安全写入队列
def update_queue_put(tag, message, color=None):
    try:
        update_queue.put_nowait((tag, message, color))
    except Queue.Full:
        logging.warning(f"更新队列满，丢弃消息: {message}")

# 启动交易
def start_trading():
    global running, selected_pairs
    if not client:
        dpg.set_value("status_label", "请先输入有效的 API 密钥")
        return
    selected_pairs = validate_pairs([pair for pair in DEFAULT_PAIRS if dpg.get_value(f"pair_{pair.replace('/', '_')}")])
    if not selected_pairs:
        dpg.set_value("status_label", "未选择有效交易对，请至少选择一个交易对")
        return
    running = True
    threading.Thread(target=trading_loop, daemon=True).start()
    dpg.set_value("status_label", "交易已启动")

# 停止交易
def stop_trading():
    global running
    running = False
    dpg.set_value("status_label", "交易已停止")

# 保存配置
def save_settings():
    global trade_speed, ma_threshold, ma_period, trade_cooldown, kline_interval
    trade_speed = dpg.get_value("trade_speed") / 100
    ma_threshold = dpg.get_value("ma_threshold") / 100
    ma_period = int(dpg.get_value("ma_period"))
    trade_cooldown = int(dpg.get_value("trade_cooldown"))
    kline_interval = dpg.get_value("kline_interval")
    dpg.set_value("status_label", "设置已保存")
    dpg.set_value("ma_label", "\n".join([f"{pair}: N/A" for pair in selected_pairs]))

# 保存 API 密钥
def save_api():
    api_key = dpg.get_value("api_key")
    api_secret = dpg.get_value("api_secret")
    if init_binance(api_key, api_secret):
        save_config(api_key, api_secret)
        update_balances()
    else:
        dpg.set_value("status_label", "无效的 API 密钥")

# 按钮点击动画回调
def button_animation(sender):
    original_color = (255, 69, 0, 255)
    highlight_color = (255, 165, 0, 255)
    with dpg.theme() as temp_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, highlight_color)
        dpg.bind_item_theme(sender, temp_theme)
    time.sleep(0.1)
    with dpg.theme() as restore_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, original_color)
        dpg.bind_item_theme(sender, restore_theme)

# 显示说明书窗口
def show_help_window():
    if dpg.does_item_exist("help_window"):
        dpg.delete_item("help_window")

    with dpg.window(label="使用说明书", tag="help_window", width=600, height=400, pos=(100, 100), no_scrollbar=False):
        dpg.add_text("稳定币 MA 套利机器人 - 使用说明书", color=(0, 255, 255))
        dpg.add_separator()
        dpg.add_text("1. 交易逻辑", color=(255, 215, 0))
        dpg.add_text(
            "本程序基于用户选择的K线周期的移动平均线（MA）进行稳定币套利，支持用户选择的交易对（如 DAI/USDT、FDUSD/USDT、USDC/USDT、XUSD/USDT、TUSD/USDT、USDP/USDT）")
        dpg.add_text("- 交易触发：")
        dpg.add_text(f"  * 当价格偏离MA超过设定阈值（默认 {ma_threshold * 100:.2f}%）时触发交易。")
        dpg.add_text("  * 卖出：价格 > MA 的稳定币，换成 USDT 或价格 < MA 的稳定币。")
        dpg.add_text("  * 买入：用 USDT 买入价格 < MA 的稳定币。")
        dpg.add_text("- 交易速度：")
        dpg.add_text("  * 偏离 > 0.05%：使用50%余额。")
        dpg.add_text(f"  * 偏离 ≤ 0.05%：使用设定比例（默认 {trade_speed * 100:.0f}%）。")
        dpg.add_text(f"- 交易频率：每 {trade_cooldown} 秒检查一次（可自定义）。")
        dpg.add_text(f"- MA周期：默认 {ma_period}，可自定义。")
        dpg.add_text(f"- K线周期：默认 {kline_interval}，可自定义（1m、5m、15m、30m、1h、4h、1d）。")
        dpg.add_separator()
        dpg.add_text("2. 使用方式", color=(255, 215, 0))
        dpg.add_text("步骤：")
        dpg.add_text("1) 在 'Binance API 设置' 中输入你的 API Key 和 API Secret，点击 '保存 API 密钥'。")
        dpg.add_text("   - 确保 API 密钥具有交易和余额读取权限。")
        dpg.add_text("2) 在 '交易对选择' 中勾选想要套利的交易对（USD1/USDT 默认不启用）。")
        dpg.add_text("3) 在 '交易设置' 中配置参数：")
        dpg.add_text("   - 单次交易比例（%）：每次交易使用多少余额。")
        dpg.add_text("   - MA 偏离阈值（%）：触发交易的最小偏离百分比。")
        dpg.add_text("   - MA 周期：计算移动平均线的K线数量。")
        dpg.add_text("   - 交易冷却时间（秒）：两次交易之间的间隔。")
        dpg.add_text("   - K线周期：选择MA计算的K线周期。")
        dpg.add_text("4) 点击 '保存设置' 确认参数(十分重要的步骤)")
        dpg.add_text("5) 点击 '启动交易' 开始自动化交易。")
        dpg.add_text("6) 点击 '停止交易' 暂停交易。")
        dpg.add_text("7) 查看 '实时数据监控' 部分，了解当前价格和MA值。")
        dpg.add_text("注意事项：")
        dpg.add_text("- 确保账户有足够的稳定币余额（USDT、USDC、FDUSD、DAI、USD1、XUSD、TUSD、USDP）。")
        dpg.add_text("- 交易日志通过状态栏显示，检查是否有错误。")
        dpg.add_text("- 每次交易最小数量为5单位，低于此数量将跳过。")
        dpg.add_separator()
        dpg.add_text("想支持树酱的话可以向以下钱包地址捐款：", color=(128, 128, 128))
        dpg.add_text("0x4FdFCfc03A5416EB5d9B85F4bad282e6DaC19783", color=(128, 128, 128))
        dpg.add_text("感谢你的支持呀（不捐也没关系，作者会自己找垃圾吃的", color=(128, 128, 128))

# DearPyGui 界面
def create_gui():
    dpg.create_context()
    icon_path = os.path.join(os.path.dirname(__file__), "jio.ico")
    if not os.path.exists(icon_path):
        print(f"错误：图标文件 {icon_path} 不存在，请确保文件放置正确")
    else:
        dpg.create_viewport(title='Stablecoin MA Arbitrage Bot', width=885, height=1000, small_icon=icon_path,
                            large_icon=icon_path)

    font_path = os.path.join(os.path.dirname(__file__), "NotoSerifCJKsc-dick.otf")
    if not os.path.exists(font_path):
        print(f"错误：字体文件 {font_path} 不存在，请确保文件放置正确")
        return

    with dpg.font_registry():
        with dpg.font(font_path, 28) as title_font:  # 标题字体28
            dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
        with dpg.font(font_path, 20) as body_font:  # 正文字体20
            dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
        dpg.bind_font(body_font)

    with dpg.theme() as global_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (20, 20, 40, 255))  # 深蓝色背景
            dpg.add_theme_color(dpg.mvThemeCol_Text, (240, 240, 255, 255))  # 柔和白色文字
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (40, 40, 60, 255))  # 深色框架
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (60, 60, 80, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (80, 80, 100, 255))
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark, (0, 255, 128, 255))  # 鲜艳绿色勾选
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, (255, 180, 0, 255))  # 橙色滑块
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, (255, 220, 0, 255))
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 4)  # 稍大内边距
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 6, 4)  # 稍大间距
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)  # 圆角框架

    with dpg.theme() as button_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (255, 85, 0, 255))  # 鲜艳橙色按钮
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (255, 120, 40, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (255, 160, 80, 255))
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 12)  # 更大圆角
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 5)

    with dpg.theme() as section_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (0, 220, 255, 255))  # 青色标题
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)

    with dpg.theme() as table_theme:
        with dpg.theme_component(dpg.mvTable):
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, (60, 60, 80, 255))  # 柔和边框
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, (50, 50, 70, 255))
            dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 4, 4)

    dpg.bind_theme(global_theme)

    with dpg.window(label="树酱提示：本软件完全免费开源你从任何渠道购买都说明被骗了", width=950, height=1000, pos=(0, 0), no_scrollbar=True):
        with dpg.group():
            title_text = "稳定币 MA 套利交易机器人"
            title_width = len(title_text) * 17
            with dpg.drawlist(width=title_width, height=35):
                for i in range(title_width):
                    t = i / title_width
                    r = int(100 + 155 * (1 - t))
                    g = int(150 + 65 * t)
                    b = int(200 + 55 * t)
                    dpg.draw_line((i, 0), (i, 35), color=(r, g, b, 255))
                dpg.draw_text((0, 0), title_text, size=28)
            dpg.bind_item_font(dpg.last_item(), title_font)

            # Binance API 设置
            with dpg.table(header_row=False, borders_outerV=True, borders_innerV=True, borders_outerH=True):
                dpg.add_table_column(width_fixed=True, width=160)
                dpg.add_table_column()
                with dpg.table_row():
                    dpg.add_text("Binance API 设置")
                    dpg.bind_item_theme(dpg.last_item(), section_theme)
                    dpg.add_spacer()
                with dpg.table_row():
                    dpg.add_text("API Key")
                    dpg.add_input_text(tag="api_key", default_value=load_config().get('api_key', ''), width=660)
                with dpg.table_row():
                    dpg.add_text("API Secret")
                    dpg.add_input_text(tag="api_secret", default_value=load_config().get('api_secret', ''), password=True, width=660)
                with dpg.table_row():
                    dpg.add_spacer()
                    dpg.add_button(label="保存 API 密钥", callback=lambda: (
                        save_api(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                    dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.bind_item_theme(dpg.last_container(), table_theme)

            # 交易对选择（折叠）
            with dpg.collapsing_header(label="交易对选择(USD1好像是这个接口还不兼容，等过两个月再试试吧)", default_open=False):
                dpg.bind_item_theme(dpg.last_item(), section_theme)
                with dpg.group(horizontal=True):
                    for i, pair in enumerate(DEFAULT_PAIRS):
                        default_value = False if pair == 'USD1/USDT' else True
                        dpg.add_checkbox(label=pair, tag=f"pair_{pair.replace('/', '_')}", default_value=default_value)
                        if i % 4 == 3:
                            dpg.add_spacer(height=8)
                            with dpg.group(horizontal=True):
                                pass
                dpg.add_button(label="更新交易对", callback=lambda: (
                    save_settings(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)

            # 实时数据监控
            with dpg.table(header_row=True, borders_outerV=True, borders_innerV=True, borders_outerH=True):
                dpg.add_table_column(width_fixed=True, width=160, label="监控项")
                dpg.add_table_column(width_fixed=True, width=360, label="价格")
                dpg.add_table_column(width_fixed=True, width=360, label=f"MA{ma_period}")
                with dpg.table_row():
                    dpg.add_text("价格/MA")
                    dpg.add_text("N/A", tag="price_label")
                    dpg.bind_item_font(dpg.last_item(), body_font)
                    dpg.add_text("N/A", tag="ma_label")
                    dpg.bind_item_font(dpg.last_item(), body_font)
                with dpg.table_row():
                    dpg.add_text("持仓")
                    dpg.add_text("N/A", tag="balance_label")
                    dpg.bind_item_font(dpg.last_item(), body_font)
                    dpg.add_spacer()
                dpg.bind_item_theme(dpg.last_container(), table_theme)

            # 交易设置
            with dpg.table(header_row=False, borders_outerV=True, borders_innerV=True, borders_outerH=True):
                dpg.add_table_column(width_fixed=True, width=160)
                dpg.add_table_column()
                with dpg.table_row():
                    dpg.add_text("交易设置")
                    dpg.bind_item_theme(dpg.last_item(), section_theme)
                    dpg.add_spacer()
                with dpg.table_row():
                    dpg.add_text("单次交易比例 (%)")
                    dpg.add_input_float(tag="trade_speed", default_value=10.0, min_value=0.0, max_value=100.0, width=220)
                with dpg.table_row():
                    dpg.add_text("MA 偏离阈值 (%)")
                    dpg.add_input_float(tag="ma_threshold", default_value=0.01, min_value=0.0, max_value=100.0, width=220)
                with dpg.table_row():
                    dpg.add_text("MA 周期")
                    dpg.add_input_int(tag="ma_period", default_value=30, min_value=1, max_value=100, width=220)
                with dpg.table_row():
                    dpg.add_text("交易冷却时间 (秒)")
                    dpg.add_input_int(tag="trade_cooldown", default_value=3600, min_value=60, max_value=86400, width=220)
                with dpg.table_row():
                    dpg.add_text("K线周期")
                    dpg.add_combo(tag="kline_interval", items=['1m', '5m', '15m', '30m', '1h', '4h', '1d'], default_value='4h', width=220)
                dpg.bind_item_theme(dpg.last_container(), table_theme)

            # 操作按钮
            with dpg.group(horizontal=True, horizontal_spacing=10):
                dpg.add_button(label="保存设置", callback=lambda: (
                    save_settings(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.add_button(label="启动交易", callback=lambda: (
                    start_trading(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.add_button(label="停止交易", callback=lambda: (
                    stop_trading(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)
                dpg.add_button(label="帮助", callback=lambda: (
                    show_help_window(), threading.Thread(target=button_animation, args=(dpg.last_item(),)).start()))
                dpg.bind_item_theme(dpg.last_item(), button_theme)

            # 状态栏
            dpg.add_text("状态: 未启动", tag="status_label")
            dpg.bind_item_font(dpg.last_item(), title_font)

    dpg.setup_dearpygui()
    dpg.show_viewport()
    while dpg.is_dearpygui_running():
        update_ui_callback()
        dpg.render_dearpygui_frame()
    dpg.destroy_context()

# 主函数
def main():
    config = load_config()
    if config.get('api_key') and config.get('api_secret'):
        init_binance(config['api_key'], config['api_secret'])
        update_balances()
    create_gui()

if __name__ == "__main__":
    main()