def run(input_data):
    text = input_data["text"]
    words = text.split()
    return {"word_count": len(words), "char_count": len(text)}
