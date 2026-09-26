![alt text](hermes-session-log.png)

LLM没有返回输出时，直接写入新的user_message;
LLM有tool_call时，补充对应的tool_result以及通过assistant记录被中断；