# Kalshi Market Making Algorithm

This project implements a market making algorithm for Kalshi markets, capable of running multiple strategies in parallel. Its most involved algorithm implementaiton is the Avellaneda-Stoikov model.

## Local Setup

1. Clone the repository
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Create a `.env` file with your Kalshi credentials:
   ```
   KALSHI_API_KEY_ID=your_api_key_id
   KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/your/private-key.key
   KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2
   ```
   Use `https://api.elections.kalshi.com/trade-api/v2` for production.
4. Create or modify the `config.yaml` file with your market making configurations. Each configuration in this file will run as a separate strategy.
5. Run the script:
   ```
   python runner.py --config config.yaml
   ```

## Configuration

The `config.yaml` file should contain one or more strategy configurations. Each strategy should have a unique name and include the following sections:

- `api`: Specifies the market ticker and trade side.
- `market_maker`: Defines parameters for the Avellaneda market maker algorithm.
- `dt`: The time step for the market maker's main loop.

Example:

```yaml
strategy_name:
  api:
    market_ticker: MARKET-TICKER
    trade_side: "yes"
  market_maker:
    max_position: 5
    order_expiration: 28800
    gamma: 0.1
    k: 1.5
    sigma: 0.001
    T: 28800
    min_spread: 0.0
    position_limit_buffer: 0.1
    inventory_skew_factor: 0.001
  dt: 2.0
```

You can define multiple strategies in the same file. The runner will execute all strategies in parallel.

## Deploying on fly.io

1. Install the flyctl CLI: https://fly.io/docs/hands-on/install-flyctl/
2. Login to fly.io:
   ```
   flyctl auth login
   ```
3. Navigate to your project directory and initialize your fly.io app:
   ```
   flyctl launch
   ```
   Follow the prompts, but don't deploy yet.
4. Set your Kalshi credentials and base URL as secrets:
   ```
   flyctl secrets set KALSHI_API_KEY_ID=your_api_key_id
   flyctl secrets set KALSHI_PRIVATE_KEY_PATH=/app/keys/kalshi-private.key
   flyctl secrets set KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2
   ```
5. Ensure your `config.yaml` file is in the project directory and contains all the strategies you want to run.
6. Deploy the app:
   ```
   flyctl deploy
   ```

The deployment will use the `runner.py` script, which will run all strategies defined in your `config.yaml` file in parallel.

## Monitoring

Each strategy will log its activities to a separate log file named after the strategy (e.g., `strategy_name.log`). You can monitor these logs using the fly.io logging system:

```
flyctl logs
```

For more detailed instructions on monitoring and managing your deployment, refer to the fly.io documentation.
