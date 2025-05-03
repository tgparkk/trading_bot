from flask import Flask, jsonify, request
from utils.database import db
from core.api_client import api_client
from core.order_manager import order_manager
from core.stock_explorer import stock_explorer
import asyncio

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "Trading Bot API is running"})

@app.route('/api/status')
def api_status():
    return jsonify(db.get_latest_system_status())

@app.route('/api/trades')
def api_trades():
    return jsonify(db.get_trades()[:50])

@app.route('/api/token_logs')
def api_token_logs():
    return jsonify(db.get_token_logs()[:50])

@app.route('/api/symbol_search_logs')
def api_symbol_search_logs():
    return jsonify(db.get_symbol_search_logs()[:50])

@app.route('/api/account')
def api_account():
    # 계좌 정보 반환
    try:
        info = api_client.get_account_balance()
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/positions')
def api_positions():
    # 보유 종목 리스트 반환
    try:
        positions = db.get_all_positions()
        return jsonify(positions)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/candidates')
def api_candidates():
    # 매수 후보 리스트 반환 (최근 탐색 결과)
    try:
        # 최근 symbol_search_logs에서 마지막 성공 결과의 filtered_symbols
        logs = db.get_symbol_search_logs()
        for log in logs:
            if log['status'] == 'SUCCESS' and log['filtered_symbols']:
                # filtered_symbols는 심볼 리스트가 아니라 개수이므로, 실제 후보는 따로 관리 필요
                # 임시로 최근 거래량 상위 종목 반환
                break
        # 실제 후보 리스트는 TradingBot에서 관리하는 것이 가장 정확
        # 여기서는 임시로 최근 거래량 상위 종목 반환
        vol_data = api_client.get_market_trading_volume()
        if vol_data.get('rt_cd') == '0':
            raw_list = vol_data['output2'][:50]
            candidates = [item['mksc_shrn_iscd'] for item in raw_list]
            return jsonify(candidates)
        return jsonify([])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/refresh_candidates', methods=['POST'])
def api_refresh_candidates():
    # 매수 후보 리스트 갱신 (비동기 트리거)
    try:
        # StockExplorer 호출
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        symbols = loop.run_until_complete(stock_explorer.get_tradable_symbols())
        loop.close()
        return jsonify({'candidates': symbols})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)
