"""
💰 Moon Dev's Funding Rate Monitor
Built with love by Moon Dev 🚀

Fran the Funding Agent tracks funding rate changes across different timeframes and announces significant changes via OpenAI TTS.
"""

import os
import pandas as pd
import time
from datetime import datetime, timedelta
from termcolor import colored, cprint
from dotenv import load_dotenv
import openai
import anthropic
from pathlib import Path
from src import nice_funcs as n
from src import nice_funcs_hl as hl
from src.agents.api import MoonDevAPI
from collections import deque
from src.agents.base_agent import BaseAgent
import traceback
import numpy as np

# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Configuration
CHECK_INTERVAL_MINUTES = 5  # How often to check funding rates
NEGATIVE_THRESHOLD = -5 # AI Run & Alert if annual rate below -1%
POSITIVE_THRESHOLD = 20  # AI Run & Alert if annual rate above 20%

# OHLCV Data Settings
TIMEFRAME = '15m'  # Candlestick timeframe
LOOKBACK_BARS = 100  # Number of candles to analyze

# Symbol to name mapping
SYMBOL_NAMES = {
    'BTC': 'Bitcoin',
    'ETH': 'Ethereum',
    'SOL': 'Solana',
    'WIF': 'Wif',
    'BNB': 'BNB',
    'FARTCOIN': 'Fart Coin'
}

# AI Settings - Override config.py if set
# Import defaults from config
from src import config

# Only set these if you want to override config.py settings
AI_MODEL = False  # Set to model name to override config.AI_MODEL
AI_TEMPERATURE = 0  # Set > 0 to override config.AI_TEMPERATURE
AI_MAX_TOKENS = 50  # Set > 0 to override config.AI_MAX_TOKENS

# Voice settings
VOICE_MODEL = "tts-1"
VOICE_NAME = "fable"  # Options: alloy, echo, fable, onyx, nova, shimmer
VOICE_SPEED = 1

# AI Analysis Prompt
FUNDING_ANALYSIS_PROMPT = """You must respond in exactly 3 lines:
Line 1: Only write BUY, SELL, or NOTHING
Line 2: One short reason why
Line 3: Only write "Confidence: X%" where X is 0-100

Analyze {symbol} with {rate}% funding rate:
{market_data}
{funding_data}

super negative funding rates in a trending market may be a good buy
super high funding rates in a downtrend may be a good sell

"""

class FundingAgent(BaseAgent):
    """Fran the Funding Rate Monitor 💰"""
    
    def __init__(self):
        """Initialize Fran the Funding Agent"""
        super().__init__('funding')
        
        # Set AI parameters - use config values unless overridden
        self.ai_model = AI_MODEL if AI_MODEL else config.AI_MODEL
        self.ai_temperature = AI_TEMPERATURE if AI_TEMPERATURE > 0 else config.AI_TEMPERATURE
        self.ai_max_tokens = AI_MAX_TOKENS if AI_MAX_TOKENS > 0 else config.AI_MAX_TOKENS
        
        print(f"🤖 Using AI Model: {self.ai_model}")
        if AI_MODEL or AI_TEMPERATURE > 0 or AI_MAX_TOKENS > 0:
            print("⚠️ Note: Using some override settings instead of config.py defaults")
            if AI_MODEL:
                print(f"  - Model: {AI_MODEL}")
            if AI_TEMPERATURE > 0:
                print(f"  - Temperature: {AI_TEMPERATURE}")
            if AI_MAX_TOKENS > 0:
                print(f"  - Max Tokens: {AI_MAX_TOKENS}")
                
        load_dotenv()
        
        # Get API keys
        openai_key = os.getenv("OPENAI_KEY")
        anthropic_key = os.getenv("ANTHROPIC_KEY")
        
        if not openai_key:
            raise ValueError("🚨 OPENAI_KEY not found in environment variables!")
        if not anthropic_key:
            raise ValueError("🚨 ANTHROPIC_KEY not found in environment variables!")
            
        openai.api_key = openai_key
        self.client = anthropic.Anthropic(api_key=anthropic_key)
        
        self.api = MoonDevAPI()
        
        # Create data directories if they don't exist
        self.audio_dir = PROJECT_ROOT / "src" / "audio"
        self.data_dir = PROJECT_ROOT / "src" / "data"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize or load historical data
        self.history_file = self.data_dir / "funding_history.csv"
        self.load_history()
        
        print("💰 Fran the Funding Agent initialized!")
        print(f"🎯 Alerting on funding rates: below {NEGATIVE_THRESHOLD}% or above {POSITIVE_THRESHOLD}%")
        print(f"📊 Analyzing {LOOKBACK_BARS} {TIMEFRAME} candles for context")
        
    def _analyze_opportunity(self, symbol, funding_data, market_data):
        """Get AI analysis of the opportunity"""
        try:
            # Prepare the context
            rate = funding_data['annual_rate'].iloc[0]
            context = FUNDING_ANALYSIS_PROMPT.format(
                symbol=symbol,
                rate=f"{rate:.2f}",
                market_data=market_data.tail(5).to_string(),  # Last 5 candles for brevity
                funding_data=funding_data.to_string()
            )
            
            print(f"\n🤖 Analyzing {symbol} with AI...")
            
            # Get AI analysis using instance settings
            message = self.client.messages.create(
                model=self.ai_model,
                max_tokens=self.ai_max_tokens,
                temperature=self.ai_temperature,
                messages=[{
                    "role": "user",
                    "content": context
                }]
            )
            
            # The response is in message.content
            if not message or not message.content:
                print("❌ No response from AI")
                return None
                
            # Handle TextBlock response
            response = message.content
            if isinstance(response, list):
                # If it's a list of TextBlocks, get the text from the first one
                if len(response) > 0 and hasattr(response[0], 'text'):
                    response = response[0].text
                else:
                    print("❌ Invalid response format from AI")
                    return None
            
            # Parse response - handle both newline and period-based splits
            lines = [line.strip() for line in response.split('\n') if line.strip()]
            if not lines:
                print("❌ Empty response from AI")
                return None
                
            # First line should be the action
            action = lines[0].strip().upper()
            if action not in ['BUY', 'SELL', 'NOTHING']:
                print(f"⚠️ Invalid action: {action}")
                return None
                
            # Rest is analysis
            analysis = '\n'.join(lines[1:]) if len(lines) > 1 else ""
            
            # Extract confidence - look for percentage in the analysis
            confidence = 50  # Default confidence
            for line in lines:
                if 'confidence' in line.lower():
                    try:
                        # Find any number followed by %
                        import re
                        matches = re.findall(r'(\d+)%', line)
                        if matches:
                            confidence = int(matches[0])
                    except:
                        print("⚠️ Could not parse confidence, using default")
            
            return {
                'action': action,
                'analysis': analysis,
                'confidence': confidence
            }
            
        except Exception as e:
            print(f"❌ Error in AI analysis: {str(e)}")
            traceback.print_exc()
            return None
            
    def _detect_significant_changes(self, current_data):
        """Detect extreme funding rates and analyze opportunities"""
        try:
            opportunities = {}
            
            for _, row in current_data.iterrows():
                try:
                    annual_rate = float(row['annual_rate'])
                    symbol = str(row['symbol'])
                    
                    if annual_rate < NEGATIVE_THRESHOLD or annual_rate > POSITIVE_THRESHOLD:
                        # Get OHLCV data silently
                        market_data = hl.get_data(
                            symbol=symbol,
                            timeframe=TIMEFRAME,
                            bars=LOOKBACK_BARS,
                            add_indicators=True
                        )
                        
                        if not market_data.empty:
                            analysis = self._analyze_opportunity(
                                symbol=symbol,
                                funding_data=row.to_frame().T,
                                market_data=market_data
                            )
                            
                            if analysis:
                                opportunities[symbol] = {
                                    'annual_rate': annual_rate,
                                    'action': analysis['action'],
                                    'analysis': analysis['analysis'],
                                    'confidence': analysis['confidence']
                                }
                            
                except Exception as e:
                    continue
            
            return opportunities if opportunities else None
            
        except Exception as e:
            return None

    def _format_announcement(self, opportunities):
        """Format funding rate changes and analysis into a speech-friendly message"""
        try:
            messages = []
            
            for symbol, data in opportunities.items():
                # Get full name from mapping
                token_name = SYMBOL_NAMES.get(symbol, symbol)
                rate = data['annual_rate']
                action = data['action']
                confidence = data['confidence']
                analysis = data['analysis'].split('\n')[0]  # Get just the first line of analysis
                
                if rate < NEGATIVE_THRESHOLD:
                    messages.append(
                        f"{token_name} has negative funding at {rate:.2f}% annual. "
                        f"AI suggests {action} with {confidence}% confidence. "
                        f"Analysis: {analysis} 🌙"
                    )
                elif rate > POSITIVE_THRESHOLD:
                    messages.append(
                        f"{token_name} has high funding at {rate:.2f}% annual. "
                        f"AI suggests {action} with {confidence}% confidence. "
                        f"Analysis: {analysis} 🌙"
                    )
                
            if messages:
                return "ayo moon dev 777! " + " | ".join(messages) + "!"
            return None
            
        except Exception as e:
            print(f"❌ Error formatting announcement: {str(e)}")
            return None
            
    def _announce(self, message):
        """Announce message using OpenAI TTS"""
        if not message:
            return
            
        try:
            print(f"\n📢 Announcing: {message}")
            
            # Generate speech
            response = openai.audio.speech.create(
                model=VOICE_MODEL,
                voice=VOICE_NAME,
                input=message,
                speed=VOICE_SPEED
            )
            
            # Save audio file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            audio_file = self.audio_dir / f"funding_alert_{timestamp}.mp3"
            
            response.stream_to_file(audio_file)
            
            # Play audio using system command
            os.system(f"afplay {audio_file}")
            
        except Exception as e:
            print(f"❌ Error in announcement: {str(e)}")

    def load_history(self):
        """Load or initialize historical funding rate data"""
        try:
            # Always start with clean history using the new format
            self.funding_history = pd.DataFrame(columns=['timestamp', 'symbol', 'funding_rate', 'annual_rate'])
            print("📝 Initialized new funding rate history")
            
            if self.history_file.exists():
                # Keep just one backup file
                backup_file = self.data_dir / "funding_history_backup.csv"
                os.rename(self.history_file, backup_file)
                print(f"📦 Backed up old history file")
                
        except Exception as e:
            print(f"❌ Error loading history: {str(e)}")
            self.funding_history = pd.DataFrame(columns=['timestamp', 'symbol', 'funding_rate', 'annual_rate'])
            
    def _get_current_funding(self):
        """Get current funding rate data"""
        try:
            print("\n🔍 Fetching fresh funding rate data...")
            df = self.api.get_funding_data()
            
            if df is not None and not df.empty:
                # Get latest data for each symbol
                current_data = df.sort_values('event_time').groupby('symbol').last().reset_index()
                
                # Ensure funding_rate and yearly_funding_rate are numeric
                numeric_cols = ['funding_rate', 'yearly_funding_rate']
                for col in numeric_cols:
                    current_data[col] = pd.to_numeric(current_data[col], errors='coerce')
                
                # Rename yearly_funding_rate to annual_rate for consistency
                current_data = current_data.rename(columns={'yearly_funding_rate': 'annual_rate'})
                
                # Print fun box with rates
                print("\n" + "╔" + "═" * 35 + "╗")
                print("║     🌙 Moon Dev's Funding Party 🎉     ║")
                print("╠" + "═" * 35 + "╣")
                print("║ Symbol │ Yearly Rate │  Status  ║")
                print("╟" + "─" * 35 + "╢")
                
                for _, row in current_data.iterrows():
                    # Get fun status emoji based on rate
                    if row['annual_rate'] > 20:
                        status = "🔥 HOT!"
                    elif row['annual_rate'] < -5:
                        status = "❄️ COLD"
                    elif row['annual_rate'] > 10:
                        status = "📈 NICE"
                    elif row['annual_rate'] < 0:
                        status = "📉 MEH"
                    else:
                        status = "😴 CHILL"
                        
                    # Truncate symbol to 4 characters
                    symbol = row['symbol'][:4]
                    print(f"║ {symbol:<4} │ {row['annual_rate']:>8.2f}% │ {status:<7} ║")
                
                print("╚" + "═" * 35 + "╝")
                print(f"\n🎯 Moon Dev is watching for rates below {NEGATIVE_THRESHOLD}% or above {POSITIVE_THRESHOLD}%")
                
                return current_data
            return None
            
        except Exception as e:
            print(f"❌ Error getting funding data: {str(e)}")
            traceback.print_exc()
            return None

    def _save_to_history(self, current_data):
        """Save current funding data to history"""
        try:
            if current_data is not None and not current_data.empty:
                # Convert to wide format with all symbols in one row
                wide_data = pd.DataFrame()
                wide_data['event_time'] = [current_data['event_time'].iloc[0]]  # Use first event_time
                
                # Add columns for each symbol's funding and annual rates
                for _, row in current_data.iterrows():
                    symbol = row['symbol']
                    wide_data[f'{symbol}_funding_rate'] = row['funding_rate']
                    wide_data[f'{symbol}_annual_rate'] = row['annual_rate']
                
                # Concatenate with existing history
                if self.funding_history.empty:
                    self.funding_history = wide_data
                else:
                    self.funding_history = pd.concat([self.funding_history, wide_data], ignore_index=True)
                
                # Drop duplicates based on event_time
                self.funding_history = self.funding_history.drop_duplicates(
                    subset=['event_time'], 
                    keep='last'
                )
                
                # Keep only last 24 hours of data
                cutoff_time = datetime.now() - timedelta(hours=24)
                self.funding_history = self.funding_history[
                    pd.to_datetime(self.funding_history['event_time']) > cutoff_time
                ]
                
                # Sort by event_time
                self.funding_history = self.funding_history.sort_values('event_time')
                
                # Save to file
                self.funding_history.to_csv(self.history_file, index=False)
                
        except Exception as e:
            print(f"❌ Error saving to history: {str(e)}")
            traceback.print_exc()

    def run_monitoring_cycle(self):
        """Run one monitoring cycle"""
        try:
            # Get current funding rates
            current_data = self._get_current_funding()
            
            if current_data is not None:
                # Save to history silently
                self._save_to_history(current_data)
                
                # Check for significant changes
                opportunities = self._detect_significant_changes(current_data)
                
                if opportunities:
                    # Format and announce changes
                    message = self._format_announcement(opportunities)
                    if message:
                        self._announce(message)
                        
                        # Print detailed analysis in the same box style
                        print("\n" + "╔" + "═" * 50 + "╗")
                        print("║          🌙 Moon Dev's Trading Signals 🎯          ║")
                        print("╠" + "═" * 50 + "╣")
                        for symbol, data in opportunities.items():
                            print(f"║  {symbol:<6} │ {data['action']:<6} │ {data['confidence']}% confident  ║")
                            analysis_lines = data['analysis'].split('\n')
                            for line in analysis_lines:
                                print(f"║  {line:<47} ║")
                        print("╚" + "═" * 50 + "╝")
                    
        except Exception as e:
            print(f"❌ Error in monitoring cycle: {str(e)}")

    def run(self):
        """Run the funding rate monitor continuously"""
        print("\n🚀 Starting funding rate monitoring...")
        
        while True:
            try:
                self.run_monitoring_cycle()
                print(f"\n💤 Sleeping for {CHECK_INTERVAL_MINUTES} minutes...")
                time.sleep(CHECK_INTERVAL_MINUTES * 60)
                
            except KeyboardInterrupt:
                print("\n👋 Fran the Funding Agent shutting down gracefully...")
                break
            except Exception as e:
                print(f"❌ Error in main loop: {str(e)}")
                time.sleep(60)  # Sleep for a minute before retrying

if __name__ == "__main__":
    agent = FundingAgent()
    agent.run()
