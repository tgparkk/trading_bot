"""
종목 탐색 및 필터링 모듈
"""
from typing import List, Dict, Any, Tuple
from datetime import datetime, timedelta
import numpy as np
from core.api_client import api_client
from utils.logger import logger
from utils.database import db
from config.settings import config

class StockExplorer:
    """종목 탐색기"""
    
    def __init__(self):
        self.config = config["trading"]
        self.filters = self.config.filters
    
    async def get_tradable_symbols(self, market_type: str = "ALL") -> List[str]:
        """거래 가능 종목 조회 (필터링 포함)
        
        Args:
            market_type (str): 시장 구분 ("ALL": 전체, "KOSPI": 코스피, "KOSDAQ": 코스닥)
        
        Returns:
            List[str]: 거래 가능 종목 목록
        """
        try:
            # 1) 거래량 순위 API 호출
            # 시장 코드 변환 (ALL: 전체, KOSPI: 코스피, KOSDAQ: 코스닥)
            market_code = {
                "ALL": "ALL", 
                "KOSPI": "KOSPI", 
                "KOSDAQ": "KOSDAQ"
            }.get(market_type, "ALL")
            
            vol_data = api_client.get_market_trading_volume(market=market_code)
            if vol_data.get("rt_cd") != "0":
                logger.log_error("거래량 순위 조회 실패")
                self._log_search_failure("거래량 순위 조회 실패")
                return []

            # 시장 상황 확인 (코스피/코스닥 방향성)
            market_direction = await self._check_market_direction()
            logger.log_system(f"시장 방향성: {market_direction}")

            raw_list = vol_data["output2"][:200]  # 상위 200개만 우선 추출

            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=self.filters["avg_vol_days"])).strftime("%Y%m%d")

            candidates = []
            for item in raw_list:
                symbol = item["mksc_shrn_iscd"]
                today_vol = int(item.get("prdy_vrss_vol", 0))

                # 필터 통과 여부 및 점수 계산 (점수가 높을수록 우선순위)
                passes, score = await self._evaluate_symbol(symbol, today_vol, start_date, end_date, market_direction)
                if not passes:
                    continue

                candidates.append((symbol, score))  # 거래량 대신 종합 점수로 정렬

            # 점수 기준 내림차순 정렬 후 상위 N개 심볼 반환
            candidates.sort(key=lambda x: x[1], reverse=True)
            result = [sym for sym, _ in candidates[: self.filters["max_symbols"]]]
            
            # 종목 탐색 로그 저장
            self._log_search_success(len(raw_list), len(result))
            
            return result
            
        except Exception as e:
            logger.log_error(e, "Failed to get tradable symbols")
            self._log_search_failure(str(e))
            return []
    
    async def _evaluate_symbol(self, symbol: str, today_vol: int, start_date: str, end_date: str, 
                              market_direction: str) -> Tuple[bool, float]:
        """종목 평가 (필터 통과 여부 및 점수 계산)"""
        # 1) 기본 필터 체크
        passes_basic = await self._passes_filters(symbol, today_vol, start_date, end_date)
        if not passes_basic:
            return False, 0
            
        # 2) 기술적 지표 계산
        technical_indicators = await self._calculate_technical_indicators(symbol)
        if not technical_indicators:
            return False, 0
            
        # 3) 종합 점수 계산
        score = self._calculate_score(technical_indicators, today_vol, market_direction)
        
        # 4) 최소 점수 체크
        if score < self.filters.get("min_score", 50):  # 기본 최소 점수 50점
            return False, 0
            
        return True, score
    
    async def _passes_filters(self, symbol: str, today_vol: int, start_date: str, end_date: str) -> bool:
        """종목이 모든 필터를 통과하는지 확인"""
        # 2) 30일 일별 시세 조회 → 거래량·종가 리스트 확보
        daily = api_client.get_daily_price(symbol, start_date, end_date)
        if daily.get("rt_cd") != "0":
            return False
            
        days = daily["output2"]
        vols = [int(d["acml_vol"]) for d in days]      # 일별 누적거래량
        closes = [float(d["stck_clpr"]) for d in days]  # 일별 종가

        # 3) 평균 거래량 필터
        avg_vol = sum(vols) / len(vols)
        if avg_vol < self.filters["min_avg_volume"]:
            return False

        # 4) 거래량 급증 필터
        if today_vol / avg_vol < self.filters["vol_spike_ratio"]:
            return False

        # 5) 현재가 필터
        price_data = api_client.get_current_price(symbol)
        if price_data.get("rt_cd") != "0":
            return False
        price = float(price_data["output"]["stck_prpr"])
        if not (self.filters["price_min"] <= price <= self.filters["price_max"]):
            return False

        # 6) 단기 변동성 필터 (절대 수익률의 평균)
        changes = [
            abs((closes[i] - closes[i-1]) / closes[i-1])
            for i in range(1, len(closes))
        ]
        volat = sum(changes) / len(changes)
        if volat > self.filters["max_volatility"]:
            return False
            
        return True
    
    async def _calculate_technical_indicators(self, symbol: str) -> Dict[str, Any]:
        """기술적 지표 계산"""
        try:
            # 최근 30일 데이터 가져오기
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
            
            daily = api_client.get_daily_price(symbol, start_date, end_date)
            if daily.get("rt_cd") != "0":
                return {}
                
            days = daily["output2"]
            closes = [float(d["stck_clpr"]) for d in days]
            highs = [float(d["stck_hgpr"]) for d in days]
            lows = [float(d["stck_lwpr"]) for d in days]
            
            # 현재가
            price_data = api_client.get_current_price(symbol)
            if price_data.get("rt_cd") != "0":
                return {}
            current_price = float(price_data["output"]["stck_prpr"])
            
            # 1. 이동평균선
            ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else 0
            ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else 0
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else 0
            
            # 2. RSI (14일)
            if len(closes) >= 15:
                delta = [closes[i] - closes[i-1] for i in range(1, len(closes))]
                gain = [max(d, 0) for d in delta]
                loss = [abs(min(d, 0)) for d in delta]
                
                avg_gain = sum(gain[-14:]) / 14
                avg_loss = sum(loss[-14:]) / 14
                
                if avg_loss == 0:
                    rsi = 100
                else:
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
            else:
                rsi = 50  # 기본값
            
            # 3. 볼린저 밴드 (20일 기준)
            if len(closes) >= 20:
                sma20 = ma20
                std20 = self._calculate_std(closes[-20:])
                upper_band = sma20 + (2 * std20)
                lower_band = sma20 - (2 * std20)
                
                # 밴드 내 위치 (0~1, 0이면 하단, 1이면 상단)
                if upper_band == lower_band:
                    band_position = 0.5
                else:
                    band_position = (current_price - lower_band) / (upper_band - lower_band)
            else:
                upper_band = lower_band = sma20 = 0
                band_position = 0.5
            
            # 4. 추세 방향 (최근 5일 종가 기울기)
            if len(closes) >= 5:
                # 단순 선형 회귀 기울기 계산
                x = list(range(5))
                y = closes[-5:]
                slope = self._calculate_slope(x, y)
                trend = "UP" if slope > 0 else "DOWN"
                trend_strength = abs(slope) / (sum(y) / len(y)) * 100  # 기울기를 평균가 대비 % 비율로
            else:
                trend = "NEUTRAL"
                trend_strength = 0
                slope = 0
            
            # 5. 캔들 패턴 (최근 3개 캔들)
            if len(closes) >= 3:
                # 양봉/음봉 패턴
                last_candles = []
                for i in range(min(3, len(days)-1), 0, -1):
                    open_price = float(days[-i]["stck_oprc"])
                    close_price = float(days[-i]["stck_clpr"])
                    candle_type = "BULLISH" if close_price > open_price else "BEARISH"
                    candle_size = abs(close_price - open_price) / open_price * 100  # 캔들 크기 (%)
                    last_candles.append({"type": candle_type, "size": candle_size})
                
                # 최근 캔들이 강한 양봉인지
                strong_bullish = (last_candles[0]["type"] == "BULLISH" and 
                                  last_candles[0]["size"] > 2.0)  # 2% 이상 상승한 양봉
            else:
                last_candles = []
                strong_bullish = False
            
            return {
                "current_price": current_price,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20,
                "rsi": rsi,
                "upper_band": upper_band,
                "lower_band": lower_band,
                "band_position": band_position,
                "trend": trend,
                "trend_strength": trend_strength,
                "slope": slope,
                "strong_bullish": strong_bullish
            }
            
        except Exception as e:
            logger.log_error(e, f"Error calculating technical indicators for {symbol}")
            return {}
    
    def _calculate_std(self, values: List[float]) -> float:
        """표준편차 계산"""
        if not values:
            return 0
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return variance ** 0.5
    
    def _calculate_slope(self, x: List[int], y: List[float]) -> float:
        """선형 회귀 기울기 계산"""
        if len(x) != len(y) or len(x) < 2:
            return 0
        n = len(x)
        x_mean = sum(x) / n
        y_mean = sum(y) / n
        
        numerator = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(n))
        denominator = sum((x[i] - x_mean) ** 2 for i in range(n))
        
        return numerator / denominator if denominator != 0 else 0
    
    async def _check_market_direction(self) -> str:
        """시장 방향성 체크 (코스피/코스닥)"""
        try:
            # 코스피 지수 조회
            kospi = api_client.get_market_index("0001")
            if kospi.get("rt_cd") != "0":
                return "NEUTRAL"
                
            # 코스닥 지수 조회
            kosdaq = api_client.get_market_index("1001")
            if kosdaq.get("rt_cd") != "0":
                return "NEUTRAL"
                
            # 전일 대비 등락률
            kospi_change = float(kospi["output"]["prdy_ctrt"])
            kosdaq_change = float(kosdaq["output"]["prdy_ctrt"])
            
            # 가중 평균 (코스피 60%, 코스닥 40%)
            weighted_change = (kospi_change * 0.6) + (kosdaq_change * 0.4)
            
            if weighted_change > 1.0:  # 1% 이상 상승
                return "STRONG_UP"
            elif weighted_change > 0.3:  # 0.3% 이상 상승
                return "UP"
            elif weighted_change < -1.0:  # 1% 이상 하락
                return "STRONG_DOWN"
            elif weighted_change < -0.3:  # 0.3% 이상 하락
                return "DOWN"
            else:
                return "NEUTRAL"
                
        except Exception as e:
            logger.log_error(e, "Error checking market direction")
            return "NEUTRAL"
    
    def _calculate_score(self, indicators: Dict[str, Any], today_vol: int, market_direction: str) -> float:
        """종목 점수 계산 (0~100)"""
        if not indicators:
            return 0
        
        score = 50  # 기본 점수
        
        # 1. RSI 점수 (과매도/과매수 영역 고려)
        rsi = indicators.get("rsi", 50)
        if 30 <= rsi <= 70:  # 중립 영역은 기본점수
            score += 0
        elif rsi < 30:  # 과매도 (매수 기회)
            score += 10
        else:  # 과매수 (70 이상)
            score -= 10
        
        # 2. 이동평균선 점수
        current_price = indicators.get("current_price", 0)
        ma5 = indicators.get("ma5", 0)
        ma10 = indicators.get("ma10", 0)
        ma20 = indicators.get("ma20", 0)
        
        if ma5 > 0 and ma10 > 0 and ma20 > 0:
            # 골든 크로스 (단기>장기)
            if ma5 > ma10 > ma20:
                score += 15
            # 현재가가 모든 이평선 위에 있으면 강한 상승세
            elif current_price > ma5 > ma10:
                score += 10
            # 데드 크로스 (단기<장기)
            elif ma5 < ma10 < ma20:
                score -= 15
        
        # 3. 볼린저 밴드 점수
        band_position = indicators.get("band_position", 0.5)
        if 0.3 <= band_position <= 0.7:  # 밴드 중간 영역
            score += 5
        elif band_position < 0.2:  # 하단 접근 (매수 기회)
            score += 15
        elif band_position > 0.8:  # 상단 접근 (과열)
            score -= 10
        
        # 4. 추세 점수
        trend = indicators.get("trend", "NEUTRAL")
        trend_strength = indicators.get("trend_strength", 0)
        
        if trend == "UP":
            score += min(15, trend_strength / 2)  # 최대 15점 가산
        elif trend == "DOWN":
            score -= min(15, trend_strength / 2)  # 최대 15점 감산
        
        # 5. 캔들 패턴 점수
        if indicators.get("strong_bullish", False):
            score += 10  # 강한 양봉 패턴
        
        # 6. 시장 방향성 고려
        if market_direction == "STRONG_UP":
            score += 10  # 강한 상승장
        elif market_direction == "UP":
            score += 5  # 상승장
        elif market_direction == "STRONG_DOWN":
            score -= 10  # 강한 하락장
        elif market_direction == "DOWN":
            score -= 5  # 하락장
        
        # 최종 점수 범위 조정 (0~100)
        return max(0, min(100, score))
    
    def _log_search_success(self, total_symbols: int, filtered_symbols: int):
        """종목 탐색 성공 로그 저장"""
        db.save_symbol_search_log(
            total_symbols=total_symbols,
            filtered_symbols=filtered_symbols,
            search_criteria=self.filters,
            status="SUCCESS"
        )
    
    def _log_search_failure(self, error_message: str):
        """종목 탐색 실패 로그 저장"""
        db.save_symbol_search_log(
            total_symbols=0,
            filtered_symbols=0,
            search_criteria=self.filters,
            status="FAIL",
            error_message=error_message
        )
    
    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """종목 정보 조회"""
        try:
            # 기본 정보
            info = api_client.get_stock_info(symbol)
            
            # 현재가
            price_data = api_client.get_current_price(symbol)
            
            if info.get("rt_cd") == "0" and price_data.get("rt_cd") == "0":
                result = {
                    "symbol": symbol,
                    "name": info["output"]["hts_kor_isnm"],
                    "current_price": float(price_data["output"]["stck_prpr"]),
                    "prev_close": float(price_data["output"]["pstc_prpr"]),
                    "change_rate": float(price_data["output"]["prdy_ctrt"]),
                    "volume": int(price_data["output"]["acml_vol"])
                }
                return result
            return {}
        except Exception as e:
            logger.log_error(e, f"Error getting symbol info: {symbol}")
            return {}

# 싱글톤 인스턴스
stock_explorer = StockExplorer() 