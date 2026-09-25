def run(input_data):
    amount = input_data["amount"]
    rate = {"USD_EUR": 0.92, "EUR_USD": 1.09}.get(input_data["pair"])
    if rate is None:
        raise ValueError(f"unsupported pair: {input_data['pair']}")
    return {"converted": round(amount * rate, 2), "rate": rate}
