import os
import sys
import logging
from flask import Flask, jsonify, request
from agent_logic import process_sheet

# Configure logging to output to Google Cloud Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health_check():
    """Simple health check endpoint to verify the service is active."""
    return "Pishi Studio CRM Automation Service is running.", 200

@app.route("/run-pipeline", methods=["POST"])
def run_pipeline():
    """
    Endpoint triggered by Cloud Scheduler to process outstanding leads.
    """
    logger.info("Automation pipeline triggered via POST request.")
    
    try:
        # Run your core CRM processing logic
        process_sheet()
        
        logger.info("Pipeline executed successfully.")
        return jsonify({
            "status": "success",
            "message": "CRM processing pipeline completed successfully."
        }), 200

    except Exception as e:
        logger.error(f"Pipeline execution failed: {str(e)}", exc_info=True)
        return jsonify({
            "status": "error",
            "message": f"An error occurred during execution: {str(e)}"
        }), 500

if __name__ == "__main__":
    # Cloud Run dynamically assigns a PORT environment variable.
    # Defaulting to 8080 if not present (local testing).
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)