#gemini language switcher for tts
# from pipecat.services.llm_service import FunctionCallParams
# from pipecat.frames.frames import TTSUpdateSettingsFrame
# from loguru import logger

# async def switch_language(params: FunctionCallParams, language: str):
#     """Switch the spoken language of the bot."""
#     lang_lower = language.lower()
    
#     # Map to Google Cloud Chirp 3 HD Voices
#     if lang_lower == "telugu":
#         lang_code = "te-IN"
#         voice_id = "te-IN-Chirp3-HD-Despina"
#     elif lang_lower == "hindi":
#         lang_code = "hi-IN"
#         voice_id = "hi-IN-Chirp3-HD-Despina"
#     else:
#         lang_code = "en-US"
#         voice_id = "en-US-Chirp3-HD-Despina"
        
#     logger.info(f"🗣️ Switching language to {language} | Voice: {voice_id}")
    
#     # FIXED: Pipecat expects 'voice' and 'language' keys
#     await params.llm.push_frame(
#         TTSUpdateSettingsFrame(settings={"language": lang_code, "voice": voice_id})
#     )
    
#     await params.result_callback({"status": f"Language switched to {language.capitalize()}."})

#bullbul v2
from pipecat.services.llm_service import FunctionCallParams
from pipecat.frames.frames import TTSUpdateSettingsFrame
from loguru import logger
from pipecat.frames.frames import CancelFrame

async def switch_language(params: FunctionCallParams, language: str):
    """Switch the spoken language of the bot."""
    lang_lower = language.lower()
    
    # Map to Sarvam language codes
    if lang_lower == "telugu":
        lang_code = "te-IN"
    elif lang_lower == "hindi":
        lang_code = "hi-IN"
    else:
        lang_code = "en-IN"
        
    logger.info(f"🗣️ Switching language to {language} | Voice: anushka")
    
    # Update Sarvam TTS settings mid-call
    await params.llm.push_frame(
        TTSUpdateSettingsFrame(settings={"language": lang_code, "voice": "anushka"})
    )
    
    await params.result_callback({"status": f"Language switched to {language.capitalize()}."})



async def end_call(params: FunctionCallParams):
    """Ends the phone call and disconnects the user."""
    logger.info("👋 LLM requested to end the call. Hanging up...")
    await params.llm.push_frame(CancelFrame())
    await params.result_callback({"status": "Call ended successfully."})