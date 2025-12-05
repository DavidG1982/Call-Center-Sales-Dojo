import streamlit as st
import google.generativeai as genai
from streamlit_gsheets import GSheetsConnection
import edge_tts
import asyncio
import PyPDF2
import pandas as pd
import json
import base64
import io
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
st.set_page_config(page_title="Sales Dojo", layout="wide", page_icon="🥋")

# Check for Secrets
required_secrets = ["GOOGLE_API_KEY", "drive"]
if not all(k in st.secrets for k in required_secrets):
    st.error("🚨 Setup Needed: Missing Google API Key or Drive Folder ID in secrets.")
    st.stop()

# Configure Gemini
# Switched to 'gemini-1.5-pro' for higher intelligence and better availability
genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])

# Initialize Session State
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0
if "roleplay_active" not in st.session_state:
    st.session_state.roleplay_active = True

# ==========================================
# 2. GOOGLE DRIVE HELPER FUNCTIONS
# ==========================================

@st.cache_resource
def get_drive_service():
    """Authenticates with Google Drive using the Service Account in secrets."""
    try:
        service_account_info = st.secrets["connections"]["gsheets"]
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive.readonly']
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        st.error(f"Failed to connect to Google Drive: {e}")
        return None

@st.cache_data(ttl=3600) 
def load_knowledge_base_from_drive(folder_id):
    """Downloads all PDFs from the specific Drive folder and extracts text."""
    service = get_drive_service()
    if not service:
        return "", []
    
    full_text = ""
    file_list_summary = []

    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
            fields="files(id, name)"
        ).execute()
        items = results.get('files', [])

        if not items:
            return "", []

        for item in items:
            # Download file
            request = service.files().get_media(fileId=item['id'])
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            
            done = False
            while done is False:
                status, done = downloader.next_chunk()

            # Extract Text using PyPDF2
            file_stream.seek(0)
            pdf_reader = PyPDF2.PdfReader(file_stream)
            file_text = ""
            for page in pdf_reader.pages:
                file_text += page.extract_text() + "\n"
            
            full_text += f"\n\n--- SOURCE: {item['name']} ---\n{file_text}"
            file_list_summary.append(item['name'])
            
    except Exception as e:
        st.error(f"Error reading from Drive: {e}")
        return "", []

    return full_text, file_list_summary

# ==========================================
# 3. APP LOGIC
# ==========================================

# LOAD KB FROM DRIVE
folder_id = st.secrets["drive"]["folder_id"]
with st.spinner("Loading Training Materials..."):
    kb_text, file_names = load_knowledge_base_from_drive(folder_id)

async def text_to_speech(text, voice):
    communicate = edge_tts.Communicate(text, voice)
    mp3_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_data += chunk["data"]
    return mp3_data

def play_audio_autoplay(audio_bytes):
    b64 = base64.b64encode(audio_bytes).decode()
    md = f"""
        <audio autoplay="true">
        <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
        </audio>
        """
    st.markdown(md, unsafe_allow_html=True)

def save_scorecard(agent_name, score, feedback):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        existing_data = conn.read(ttl=0)
        new_row = pd.DataFrame([{
            "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Agent Name": agent_name,
            "Score": score,
            "Feedback": feedback
        }])
        updated_df = pd.concat([existing_data, new_row], ignore_index=True)
        conn.update(data=updated_df)
        st.success("✅ Scorecard saved!")
    except Exception as e:
        st.error(f"Failed to save score: {e}")

# ==========================================
# 4. SIDEBAR
# ==========================================
with st.sidebar:
    st.title("🥋 Dojo Settings")
    agent_name = st.text_input("Agent Name", placeholder="Enter your name")
    
    st.divider()
    st.subheader("📚 Knowledge Base")
    if file_names:
        st.success(f"Loaded {len(file_names)} Files")
        with st.expander("View Files"):
            for f in file_names:
                st.write(f"📄 {f}")
        
        if st.button("🔄 Refresh Drive Files"):
            st.cache_data.clear()
            st.rerun()
    else:
        st.warning("No PDFs found in the connected Drive Folder.")

    st.divider()
    st.subheader("🔊 Audio Settings")
    voice_option = st.selectbox(
        "AI Voice",
        ["en-US-ChristopherNeural", "en-US-JennyNeural", "en-US-GuyNeural", "en-US-AriaNeural"],
        index=0
    )
    
    mode = st.radio("Select Training Mode", ["Roleplay as Realtor", "Roleplay as Homebuyer"])
    
    if st.button("Reset Session"):
        st.session_state.chat_history = []
        st.session_state.turn_count = 0
        st.session_state.roleplay_active = True
        st.rerun()

# ==========================================
# 5. MODE 1: ROLEPLAY AS REALTOR
# ==========================================
if mode == "Roleplay as Realtor":
    st.title("🏡 Roleplay as Realtor")
    st.markdown("You are the **Realtor**. The AI is a **Skeptical Buyer**. Keep the deal alive!")
    
    if not agent_name:
        st.warning("Please enter your Agent Name in the sidebar to begin.")
        st.stop()

    if not kb_text:
        st.error("⚠️ Knowledge Base is empty. Add PDFs (converted from Word) to Drive.")

    # Progress Bar
    progress = st.session_state.turn_count / 10
    st.progress(progress, text=f"Turn {st.session_state.turn_count}/10")

    audio_input = st.audio_input("Record your pitch/response")

    if audio_input and st.session_state.roleplay_active:
        if st.session_state.turn_count < 10:
            with st.spinner("The Buyer is thinking..."):
                audio_bytes = audio_input.read()
                
                # CHANGED TO GEMINI 1.5 PRO (Smarter & More Stable than Flash)
                model = genai.GenerativeModel("gemini-1.5-pro")
                
                # Limit context for Pro model just to be safe (though Pro handles 1M+ tokens)
                context_safe = kb_text[:900000] 
                
                system_prompt = f"""
                You are a SKEPTICAL HOME BUYER. The user is a Realtor/ISA.
                
                CONTEXT FROM TRAINING MATERIALS (Books & Questions List):
                {context_safe} 
                
                INSTRUCTIONS:
                1. The context includes a list of 120+ questions/objections.
                2. Silently select 10 RANDOM objections from that list to use for this session.
                3. Respond naturally as the buyer using one of those objections if it fits the flow, or make up a relevant one.
                4. CRITICAL: In the 'suggested_response' field, write exactly what the Realtor SHOULD have said to handle your last objection perfectly.
                
                OUTPUT JSON:
                {{
                    "response_text": "Your spoken response as the buyer",
                    "coach_hints": ["Hint 1 on tone", "Hint 2 on strategy"],
                    "suggested_response": "The perfect script/rebuttal the agent SHOULD have used."
                }}
                """
                
                history_context = "\n".join([f"{x['role']}: {x['content']}" for x in st.session_state.chat_history])
                full_prompt = f"{system_prompt}\n\nHISTORY:\n{history_context}\n\nRespond to the audio input."

                try:
                    response = model.generate_content(
                        [full_prompt, {"mime_type": "audio/wav", "data": audio_bytes}],
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    response_json = json.loads(response.text)
                    ai_text = response_json["response_text"]
                    hints = response_json["coach_hints"]
                    better_response = response_json.get("suggested_response", "No suggestion available.")
                    
                    tts_audio = asyncio.run(text_to_speech(ai_text, voice_option))
                    
                    st.session_state.chat_history.append({"role": "Agent", "content": "(Audio Input)"})
                    st.session_state.chat_history.append({"role": "Buyer", "content": ai_text})
                    st.session_state.turn_count += 1
                    
                    col1, col2 = st.columns([2, 1])
                    with col1:
                        st.info(f"**Buyer says:** {ai_text}")
                        play_audio_autoplay(tts_audio)
                    with col2:
                        st.markdown("### 🧠 Live Coaching")
                        with st.expander("💡 See Suggested Answer", expanded=True):
                            st.success(f"**What you should have said:**\n\n{better_response}")
                        
                        st.write("**Tips:**")
                        for hint in hints:
                            st.caption(f"• {hint}")
                            
                except Exception as e:
                    st.error(f"AI Error: {e}")

    # End Game
    if st.session_state.turn_count >= 10 and st.session_state.roleplay_active:
        st.session_state.roleplay_active = False
        st.divider()
        st.header("🏁 Session Over!")
        
        with st.spinner("Grading your performance..."):
            model = genai.GenerativeModel("gemini-1.5-pro")
            grading_prompt = f"""
            Review this sales call based on the Training Material.
            
            TRAINING MATERIAL:
            {kb_text[:200000]}
            
            HISTORY:
            {st.session_state.chat_history}
            
            OUTPUT JSON:
            {{
                "score": (integer 0-10),
                "feedback_summary": "Summary of performance."
            }}
            """
            
            grad_resp = model.generate_content(grading_prompt, generation_config={"response_mime_type": "application/json"})
            grad_json = json.loads(grad_resp.text)
            
            final_score = grad_json["score"]
            final_feedback = grad_json["feedback_summary"]
            
            st.metric("Final Score", f"{final_score}/10")
            st.write(final_feedback)
            
            if st.button("Save Result to Scorecard"):
                save_scorecard(agent_name, final_score, final_feedback)

# ==========================================
# 6. MODE 2: ROLEPLAY AS HOMEBUYER
# ==========================================
elif mode == "Roleplay as Homebuyer":
    st.title("🎓 Roleplay as Homebuyer")
    st.markdown("You act as the **Buyer**. Throw objections! The AI acts as the **Perfect Realtor** using the Books.")
    
    audio_input_mc = st.audio_input("State your objection")
    
    if audio_input_mc and kb_text:
        with st.spinner("Formulating perfect rebuttal..."):
            audio_bytes_mc = audio_input_mc.read()
            model = genai.GenerativeModel("gemini-1.5-pro")
            
            system_prompt_mc = f"""
            You are the PERFECT REALTOR based on the training books provided.
            
            CONTEXT BOOKS:
            {kb_text[:900000]}
            
            INSTRUCTIONS:
            1. Handle the objection perfectly based on the text provided.
            2. Output JSON:
            {{
                "rebuttal_text": "What you say to the buyer",
                "why_it_works": "Explanation of the sales technique used."
            }}
            """
            
            try:
                response_mc = model.generate_content(
                    [system_prompt_mc, {"mime_type": "audio/wav", "data": audio_bytes_mc}],
                    generation_config={"response_mime_type": "application/json"}
                )
                
                resp_json_mc = json.loads(response_mc.text)
                rebuttal = resp_json_mc["rebuttal_text"]
                explanation = resp_json_mc["why_it_works"]
                
                tts_audio_mc = asyncio.run(text_to_speech(rebuttal, voice
