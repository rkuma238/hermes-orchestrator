def run(input_data):
    message = input_data["message"]
    return {"echo": message, "length": len(message)}
