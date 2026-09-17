from langchain.chat_models import init_chat_model

if __name__ == "__main__":
    model = init_chat_model("gpt-5.6-luna")
    response = model.invoke("What does mean temperature in the LLM context?")
    print(response)