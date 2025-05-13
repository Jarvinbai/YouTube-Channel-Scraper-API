from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import json
import re
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
import logging
from bs4 import BeautifulSoup
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="YouTube Channel Scraper API",
    description="API to fetch videos from a YouTube channel without API key",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Models
class VideoItem(BaseModel):
    video_id: str
    title: str
    thumbnail_url: Optional[str] = None
    published_at: Optional[str] = None
    view_count: Optional[str] = None
    duration: Optional[str] = None
    url: str

class ChannelVideosResponse(BaseModel):
    channel_id: str
    channel_name: Optional[str] = None
    videos: List[VideoItem]
    continuation_token: Optional[str] = None

async def fetch_initial_channel_data(channel_id: str) -> Dict[str, Any]:
    """Fetch the initial HTML page for a YouTube channel."""
    channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(channel_url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            return {"success": True, "html": response.text}
        except httpx.HTTPStatusError as e:
            logger.error(f"Error fetching channel page: {str(e)}")
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Channel with ID {channel_id} not found")
            else:
                raise HTTPException(status_code=e.response.status_code, detail=f"Failed to fetch channel page: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")

def extract_video_data_from_html(html_content: str, channel_id: str) -> Dict[str, Any]:
    """Extract video data from the YouTube channel HTML."""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Try to get channel name
    channel_name = None
    try:
        channel_meta = soup.find('meta', property='og:title')
        if channel_meta:
            channel_name = channel_meta['content']
    except Exception as e:
        logger.warning(f"Failed to extract channel name: {e}")
    
    # Extract initial data that contains video information
    data = None
    scripts = soup.find_all('script')
    for script in scripts:
        if script.string and 'ytInitialData' in script.string:
            json_str = re.search(r'var ytInitialData = (.+?);</script>', str(script), re.DOTALL)
            if json_str:
                try:
                    data = json.loads(json_str.group(1))
                    break
                except json.JSONDecodeError:
                    continue
    
    if not data:
        logger.error("Could not extract ytInitialData from page")
        raise HTTPException(status_code=500, detail="Failed to extract video data from YouTube page")
    
    videos = []
    continuation_token = None
    
    try:
        # Navigate through the JSON structure to find video items
        # This is a complex structure and might change over time as YouTube updates their site
        tabs = data.get('contents', {}).get('twoColumnBrowseResultsRenderer', {}).get('tabs', [])
        for tab in tabs:
            if 'tabRenderer' in tab and tab['tabRenderer'].get('title') == 'Videos':
                video_items = tab['tabRenderer'].get('content', {}).get('richGridRenderer', {}).get('contents', [])
                
                # Process each video item
                for item in video_items:
                    if 'richItemRenderer' in item:
                        video_data = item['richItemRenderer'].get('content', {}).get('videoRenderer', {})
                        if video_data:
                            try:
                                video_id = video_data.get('videoId')
                                if not video_id:
                                    continue
                                
                                title = video_data.get('title', {}).get('runs', [{}])[0].get('text', 'Untitled Video')
                                
                                thumbnail_url = None
                                thumbnails = video_data.get('thumbnail', {}).get('thumbnails', [])
                                if thumbnails:
                                    thumbnail_url = thumbnails[-1].get('url')  # Get highest quality thumbnail
                                
                                published_text = video_data.get('publishedTimeText', {}).get('simpleText', '')
                                
                                view_count_text = ''
                                view_count_item = video_data.get('viewCountText', {})
                                if 'simpleText' in view_count_item:
                                    view_count_text = view_count_item.get('simpleText', '')
                                elif 'runs' in view_count_item and len(view_count_item['runs']) > 0:
                                    view_count_text = view_count_item['runs'][0].get('text', '')
                                
                                duration = video_data.get('lengthText', {}).get('simpleText', '')
                                
                                videos.append(VideoItem(
                                    video_id=video_id,
                                    title=title,
                                    thumbnail_url=thumbnail_url,
                                    published_at=published_text,
                                    view_count=view_count_text,
                                    duration=duration,
                                    url=f"https://www.youtube.com/watch?v={video_id}"
                                ))
                            except Exception as e:
                                logger.warning(f"Error processing video item: {e}")
                                continue
                    
                    # Look for continuation token for pagination
                    if 'continuationItemRenderer' in item:
                        continuation_data = item['continuationItemRenderer'].get('continuationEndpoint', {}).get('continuationCommand', {})
                        continuation_token = continuation_data.get('token')
                
                break
    except Exception as e:
        logger.error(f"Error parsing video data: {e}")
        # Continue with whatever videos we've managed to extract
    
    return {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "videos": videos,
        "continuation_token": continuation_token
    }

@app.get(
    "/api/channel/{channel_id}/videos",
    response_model=ChannelVideosResponse,
    summary="Get videos from a YouTube channel",
    description="Fetches videos from a specified YouTube channel and returns them in JSON format without requiring an API key."
)
async def get_channel_videos(
    channel_id: str,
    max_results: int = Query(20, ge=1, le=50, description="Maximum number of results to return")
):
    """
    Get videos from a YouTube channel by channel ID without using the YouTube API.
    
    - **channel_id**: YouTube channel ID
    - **max_results**: Maximum number of videos to return (1-50)
    """
    try:
        # First fetch the channel page
        channel_data = await fetch_initial_channel_data(channel_id)
        
        # Extract video data from the HTML
        result = extract_video_data_from_html(channel_data["html"], channel_id)
        
        # Limit the number of videos based on max_results
        result["videos"] = result["videos"][:max_results]
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_channel_videos: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"An error occurred while fetching videos: {str(e)}"
        )

@app.get("/", include_in_schema=False)
async def root():
    return {"message": "YouTube Channel Scraper API. Access /docs for API documentation."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)