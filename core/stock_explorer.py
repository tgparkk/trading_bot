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
        """거래 가능 종목 조회 (거래량 상위 종목만 반환)
        
        Args:
            market_type (str): 시장 구분 ("ALL": 전체, "KOSPI": 코스피, "KOSDAQ": 코스닥)
        
        Returns:
            List[str]: 거래량 상위 종목 목록
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
            
            # API 응답 로깅 (오류 발생 시에만)
            if vol_data.get("rt_cd") != "0":
                logger.log_system(f"API 응답 구조: {list(vol_data.keys())}")
            
            if vol_data.get("rt_cd") != "0":
                logger.log_error("거래량 순위 조회 실패: " + vol_data.get("msg_cd", "알 수 없는 오류"))
                self._log_search_failure("거래량 순위 조회 실패: " + vol_data.get("msg_cd", "알 수 없는 오류"))
                return []

            # 응답 키 확인 및 데이터 추출
            output_key = None
            for key in vol_data.keys():
                if key.startswith("output"):
                    output_key = key
                    break
                    
            if not output_key:
                logger.log_system("API 응답 전체 내용: " + str(vol_data))
                logger.log_error("거래량 순위 응답에 output 키가 없습니다")
                self._log_search_failure("거래량 순위 응답에 output 키가 없습니다")
                return []
                
            # 상위 종목 추출
            max_symbols = self.filters.get("max_symbols", 100)
            
            # 응답 구조에 따라 데이터 추출 방식 변경
            if isinstance(vol_data[output_key], list):
                # 리스트 형태인 경우
                raw_list = vol_data[output_key][:max_symbols]
                # 심볼 코드 필드명 확인
                if len(raw_list) > 0:
                    sample_item = raw_list[0]
                    symbol_field = None
                    for field in sample_item.keys():
                        if "iscd" in field.lower() or "code" in field.lower() or "symbol" in field.lower():
                            symbol_field = field
                            break
                    
                    if symbol_field:
                        symbols = [item[symbol_field] for item in raw_list]
                    else:
                        logger.log_system("첫 번째 항목 구조: " + str(sample_item))
                        logger.log_error("심볼 코드 필드를 찾을 수 없습니다")
                        self._log_search_failure("심볼 코드 필드를 찾을 수 없습니다")
                        return []
                else:
                    logger.log_error("거래량 순위 데이터가 비어 있습니다")
                    self._log_search_failure("거래량 순위 데이터가 비어 있습니다")
                    return []
            else:
                # 리스트가 아닌 경우 (딕셔너리 등)
                logger.log_system("API 응답 output 구조: " + str(vol_data[output_key]))
                logger.log_error("예상치 못한 API 응답 구조")
                self._log_search_failure("예상치 못한 API 응답 구조")
                return []
            
            # 종목 스캔 결과 요약 기록 - 매번 상세 로그 대신 요약만 남김
            scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.log_system(f"거래량 상위 {len(symbols)}개 종목을 선택했습니다. ({market_type})")
            
            # 전체 스캔 결과 요약 로그 (trade.log에 기록)
            logger.log_trade(
                action="SCAN",
                symbol="ALL",
                price=0,
                quantity=len(symbols),
                reason=f"[{market_type}] 거래량 기준 종목 스캔",
                scan_time=scan_time,
                top_symbols=", ".join(symbols[:5])  # 상위 5개만 기록하여 로그 크기 감소
            )
            
            # 상위 5개 종목만 로그에 남김 (10개에서 축소)
            for symbol in symbols[:5]:
                try:
                    symbol_info = self.get_symbol_info(symbol)
                    if symbol_info:
                        # 각 종목별 상세 정보 로그 (trade.log에 기록)
                        logger.log_trade(
                            action="SYMBOL_INFO",
                            symbol=symbol,
                            price=symbol_info.get("current_price", 0),
                            quantity=0,
                            reason="종목 스캔",
                            name=symbol_info.get("name", ""),
                            volume=symbol_info.get("volume", 0),
                            change_rate=f"{symbol_info.get('change_rate', 0):.2f}%"
                        )
                except Exception as e:
                    logger.log_error(e, f"종목 {symbol} 정보 로깅 실패")
            
            # 종목 탐색 로그 저장
            self._log_search_success(len(raw_list), len(symbols))
            
            return symbols
            
        except Exception as e:
            logger.log_error(e, "Failed to get tradable symbols")
            self._log_search_failure(str(e))
            return []
    
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
    
    def _log_search_success(self, total_symbols: int, filtered_symbols: int):
        """종목 탐색 성공 로그 저장"""
        db.save_symbol_search_log(
            total_symbols=total_symbols,
            filtered_symbols=filtered_symbols,
            search_criteria={"method": "volume_ranking"},
            status="SUCCESS"
        )
    
    def _log_search_failure(self, error_message: str):
        """종목 탐색 실패 로그 저장"""
        db.save_symbol_search_log(
            total_symbols=0,
            filtered_symbols=0,
            search_criteria={"method": "volume_ranking"},
            status="FAIL",
            error_message=error_message
        )
    
    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """종목 정보 조회"""
        try:
            # 기본 정보
            info = api_client.get_stock_info(symbol)
            
            # API 응답 구조 로깅 (오류 발생 시에만)
            if info.get("rt_cd") != "0":
                logger.log_system(f"종목 정보 API 응답 구조: {list(info.keys())}")
                if "output" in info:
                    logger.log_system(f"종목 정보 output 구조: {list(info['output'].keys())}")
            
            # 현재가
            price_data = api_client.get_current_price(symbol)
            
            if info.get("rt_cd") == "0" and price_data.get("rt_cd") == "0":
                # 종목명 필드 찾기
                name_field = None
                name = "Unknown"
                
                if "output" in info:
                    output = info["output"]
                    # 가능한 이름 필드들을 순서대로 확인
                    for field in ["hts_kor_isnm", "prdt_name", "stck_prdt_name", "kor_name", "name"]:
                        if field in output:
                            name = output[field]
                            name_field = field
                            break
                    
                    # 필드 이름에 "name"이 포함된 키를 검색
                    if not name_field:
                        for field in output.keys():
                            if "name" in field.lower() or "nm" in field.lower():
                                name = output[field]
                                name_field = field
                                break
                
                # 가격 정보 필드 찾기
                current_price = 0
                volume = 0
                change_rate = 0
                prev_close = 0
                
                if "output" in price_data:
                    price_output = price_data["output"]
                    
                    # 현재가 필드 찾기
                    for field in ["stck_prpr", "current_price", "price", "prpr"]:
                        if field in price_output:
                            current_price = float(price_output[field])
                            break
                    
                    # 거래량 필드 찾기
                    for field in ["acml_vol", "volume", "vol"]:
                        if field in price_output:
                            volume = int(price_output[field])
                            break
                    
                    # 등락률 필드 찾기
                    for field in ["prdy_ctrt", "change_rate", "prdy_vrss_prpr_rate"]:
                        if field in price_output:
                            change_rate = float(price_output[field])
                            break
                    
                    # 전일종가 필드 찾기
                    for field in ["pstc_prpr", "prev_close", "stck_prdy_clpr"]:
                        if field in price_output:
                            prev_close = float(price_output[field])
                            break
                
                # 결과 조합
                result = {
                    "symbol": symbol,
                    "name": name, 
                    "current_price": current_price,
                    "prev_close": prev_close,
                    "change_rate": change_rate,
                    "volume": volume
                }
                return result
                
            # API 오류 시 빈 결과 반환 이전에 오류 로깅
            if info.get("rt_cd") != "0":
                error_msg = f"API 응답 오류: {info.get('msg1', '알 수 없는 오류')} (rt_cd: {info.get('rt_cd')})"
                logger.log_system(error_msg)
            return {}
            
        except Exception as e:
            logger.log_error(e, f"Error getting symbol info: {symbol}")
            return {
                "symbol": symbol,
                "name": f"Unknown({symbol})",
                "current_price": 0,
                "prev_close": 0, 
                "change_rate": 0,
                "volume": 0
            }

    async def get_top_volume_stocks(self, market: str = None, limit: int = 20) -> List[Dict[str, Any]]:
        """거래량 상위 종목 조회 및 상세 정보 반환
        
        Args:
            market (str): 시장 구분 (None: 전체, "KOSPI": 코스피, "KOSDAQ": 코스닥)
            limit (int): 조회할 종목 수 (기본값: 20)
            
        Returns:
            List[Dict[str, Any]]: 거래량 상위 종목 정보 목록 (심볼, 이름, 현재가, 등락률, 거래량 등)
        """
        try:
            # 토큰 상태 확인
            if api_client.is_token_valid(min_hours=1.0):
                logger.log_system("거래량 상위 종목 조회 - 기존 토큰이 유효합니다. 재발급 없이 진행합니다.")
            else:
                logger.log_system("거래량 상위 종목 조회 - 토큰이 만료되었거나 유효하지 않습니다. 토큰 재발급이 필요합니다.")
            
            # market 매개변수 처리 - None이면 "ALL"로 변환
            market_type = "ALL" if market is None else market
            
            # 거래량 상위 종목 가져오기
            symbols = await self.get_tradable_symbols(market_type=market_type)
            
            if not symbols:
                logger.log_system("거래량 상위 종목을 찾을 수 없습니다.")
                return []
            
            # 상위 종목만 선택 (limit 개수만큼)
            selected_symbols = symbols[:min(limit, len(symbols))]
            
            # 종목별 상세 정보 조회
            result = []
            for symbol in selected_symbols:
                symbol_info = self.get_symbol_info(symbol)
                if symbol_info:  # 정보 조회 성공 시에만 추가
                    result.append(symbol_info)
            
            # 로그 기록
            logger.log_system(f"거래량 상위 {len(result)}/{len(symbols)}개 종목 정보 조회 완료 (market: {market_type})")
            
            return result
            
        except Exception as e:
            logger.log_error(e, "거래량 상위 종목 조회 실패")
            return []

# 싱글톤 인스턴스
stock_explorer = StockExplorer() 