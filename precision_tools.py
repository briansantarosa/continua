# [CONTINUA FORK — from ~/Sagent @ e276913, 2026-09-07]
import requests
import yfinance as yf
import logging

logger = logging.getLogger(__name__)

class PrecisionTools:
    @staticmethod
    def get_stock_info(symbol: str) -> str:
        """Fetch real-time stock data for a given ticker symbol (e.g., 'AAPL', 'SPY')."""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            current_price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose') or info.get('regularMarket Previous Close')
            currency = info.get('currency', 'USD')
            day_high = info.get('dayHigh')
            day_low = info.get('dayLow')
            
            return f"Stock {symbol}: Current Price: {current_price} {currency}, Day High: {day_high}, Day Low: {day_low}."
        except Exception as e:
            logger.error(f"yfinance error for {symbol}: {e}")
            return f"Could not retrieve stock data for {symbol}."

    @staticmethod
    def get_weather(city: str) -> str:
        """Fetch current weather forecast for a given city using Open-Meteo."""
        try:
            # Geocoding city to lat/lon via Nominatim (Free, no key)
            geo_url = f"https://nominatim.openstreetmap.org/search?format=json&q={city}"
            headers = {'User-Agent': 'SagentWeatherTool/1.0'}
            geo_resp = requests.get(geo_url, headers=headers).json()
            
            if not geo_resp:
                return f"Could not find location for city: {city}."
            
            lat = geo_resp[0]['lat']
            lon = geo_resp[0]['lon']
            
            # Open-Meteo API (Free, no key)
            weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
            w_resp = requests.get(weather_url).json()
            
            current = w_resp.get('current_weather', {})
            temp = current.get('temperature')
            windspeed = current.get('windspeed')
            
            return f"Current weather in {city}: Temperature: {temp}C, Windspeed: {windspeed} km/h."
        except Exception as e:
            logger.error(f"Weather error for {city}: {e}")
            return f"Could not retrieve weather for {city}."

def execute_precision_tool(tool_name: str, args: dict) -> str:
    pt = PrecisionTools()
    if tool_name == "get_stock_info":
        return pt.get_stock_info(args.get("symbol", ""))
    elif tool_name == "get_weather":
        return pt.get_weather(args.get("city", ""))
    return "Tool not found."
