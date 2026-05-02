#!/usr/bin/env python3
"""
Test script to verify live trading mode configuration.
This helps debug the live trading issue reported in GitHub issues.
"""

import sys
from src.config.settings import settings

def test_live_mode_config():
    """Test the live trading mode configuration."""
    print("=" * 60)
    print("LIVE TRADING MODE CONFIGURATION TEST")
    print("=" * 60)
    
    # Test default settings
    print(f"Default live_trading_enabled: {settings.trading.live_trading_enabled}")
    print(f"Default paper_trading_mode: {settings.trading.paper_trading_mode}")
    
    # Test setting live mode programmatically (like BeastModeBot does)
    print("\nTesting live mode activation:")
    settings.trading.live_trading_enabled = True
    settings.trading.paper_trading_mode = False
    
    print(f"After setting live=True: live_trading_enabled={settings.trading.live_trading_enabled}")
    print(f"After setting live=True: paper_trading_mode={settings.trading.paper_trading_mode}")
    
    # Test getattr access (like strategies do)
    live_mode_from_getattr = getattr(settings.trading, 'live_trading_enabled', False)
    print(f"getattr result: {live_mode_from_getattr}")
    
    # Test setting paper mode
    print("\nTesting paper mode activation:")
    settings.trading.live_trading_enabled = False
    settings.trading.paper_trading_mode = True
    
    print(f"After setting paper=True: live_trading_enabled={settings.trading.live_trading_enabled}")
    print(f"After setting paper=True: paper_trading_mode={settings.trading.paper_trading_mode}")
    
    live_mode_from_getattr = getattr(settings.trading, 'live_trading_enabled', False)
    print(f"getattr result: {live_mode_from_getattr}")
    
    print("\n" + "=" * 60)
    print("Configuration test completed!")
    print("If all values show correctly, the live trading bug is fixed.")
    print("=" * 60)

if __name__ == "__main__":
    test_live_mode_config()