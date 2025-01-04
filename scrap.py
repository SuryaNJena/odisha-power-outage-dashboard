from io import StringIO
import requests
from bs4 import BeautifulSoup
import pandas as pd
import os
from pathlib import Path


def extract_tables_from_url(url):
    """
    Extracts all tables from a given URL and returns them as a list of Pandas DataFrames.

    Args:
    url (str): The URL of the webpage.

    Returns:
    list[pd.DataFrame]: A list containing tables as pandas DataFrames
    """
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for bad status codes (404, 500, etc)
    except requests.exceptions.RequestException as e:
        print(f"Error fetching the URL: {e}")
        return []

    soup = BeautifulSoup(response.content, "html.parser")

    tables = soup.find_all("table")  # Find all <table> tags

    extracted_tables = []
    for table in tables:
        try:
            # Attempt to extract the table using pandas
            df = pd.read_html(StringIO(str(table)))[0]  # read_html expects a string
            extracted_tables.append(df)

        except ValueError:
            print(f"Could not parse table. Skipping...")  # Most common error to handle
            # Handle tables that aren't in the expected format.
        except Exception as e:
            print(f"Error processing table: {e}. Skipping...")
            # handle any other unexpected errors

    return extracted_tables


def process_urls_from_file(file_path, output_folder="scrappedData"):
    """
    Reads URLs from a file, processes them, and saves extracted tables as CSVs.

    Args:
        file_path (str): The path to the file containing URLs (one URL per line).
        output_folder (str, optional): The folder to save CSVs into. Defaults to "scrappedData".
    """
    try:
        with open(file_path, "r") as file:
            urls = [line.strip() for line in file if line.strip()]  # Read URLs, remove blanks
    except FileNotFoundError:
        print(f"Error: File not found at {file_path}")
        return

    Path(output_folder).mkdir(parents=True, exist_ok=True)  # Create output folder if it doesn't exist

    for url_index, url in enumerate(urls):
        print(f"\n--- Processing URL: {url} ---")
        table_data = extract_tables_from_url(url)

        if table_data:
            for table_index, df in enumerate(table_data):
                file_name = f"url_{url_index+1}_table_{table_index + 1}.csv"  # Construct a file name
                file_path = os.path.join(output_folder, file_name) # construct the full file path
                try:
                     df.to_csv(file_path, index=False, encoding="utf-8")  # Save to CSV
                     print(f"Saved table {table_index + 1} to: {file_path}")
                except Exception as e:
                    print(f"Error saving table to {file_path}: {e}")

        else:
            print("No tables found or there was an error extracting them")

if __name__ == '__main__':
    # Path to the file containing URLs (create this file!)
    urls_file_path = "urls.txt"  # Replace with your actual file path
    output_folder_name = "scrappedData" # The name of the output folder

    process_urls_from_file(urls_file_path, output_folder_name)