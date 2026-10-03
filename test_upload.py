"""
test_upload.py
--------------
CLI script to immediately test-upload a video to YouTube with overlays & thumbnail.

Usage:
    python test_upload.py
    python test_upload.py --video-id 1 --privacy unlisted
    python test_upload.py --privacy private --discord
"""

import argparse
import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select

from core.database import get_db_context
from models.video import Video
from scheduler.jobs import test_upload_video


async def main():
    parser = argparse.ArgumentParser(description="Test upload video to YouTube")
    parser.add_argument(
        "--video-id",
        type=int,
        default=None,
        help="Video ID to upload (defaults to first active video)",
    )
    parser.add_argument(
        "--privacy",
        type=str,
        default="unlisted",
        choices=["unlisted", "private", "public"],
        help="YouTube privacy status (default: unlisted)",
    )
    parser.add_argument(
        "--discord",
        action="store_true",
        help="Send Discord announcement if configured",
    )
    parser.add_argument(
        "--delete",
        type=str,
        default=None,
        metavar="YT_ID",
        help="Delete a video from YouTube by YouTube video ID (or 'latest' for the most recent upload)",
    )
    args = parser.parse_args()

    if args.delete:
        target_yt_id = args.delete.strip()
        from models.channel import YouTubeChannel
        from models.upload_history import UploadHistory
        from services.youtube import build_youtube_service_from_db

        async with get_db_context() as db:
            if target_yt_id.lower() == "latest":
                hist = (await db.execute(
                    select(UploadHistory)
                    .where(UploadHistory.youtube_video_id.isnot(None))
                    .order_by(UploadHistory.id.desc())
                )).scalar_one_or_none()
                if not hist:
                    print("❌ No uploaded video found in history.")
                    sys.exit(1)
                target_yt_id = hist.youtube_video_id
                print(f"🎯 Found latest upload: '{hist.video_title}' (YT ID: {target_yt_id})")

            # Find channel
            ch = (await db.execute(
                select(YouTubeChannel).where(YouTubeChannel.is_active == True)
            )).scalar_one_or_none()
            if not ch or not ch.is_authenticated:
                print("❌ No active authenticated YouTube channel found.")
                sys.exit(1)

            print(f"🗑️ Deleting video '{target_yt_id}' from YouTube channel '{ch.name}'...")
            try:
                yt_svc = build_youtube_service_from_db(ch)
                deleted = yt_svc.delete_video(target_yt_id)
                # Purge from upload history
                hists = (await db.execute(
                    select(UploadHistory).where(UploadHistory.youtube_video_id == target_yt_id)
                )).scalars().all()
                for h in hists:
                    await db.delete(h)
                await db.commit()
                print(f"✅ Successfully deleted '{target_yt_id}' from YouTube and removed from upload history!")
            except Exception as e:
                print(f"❌ Failed to delete video: {e}")
                sys.exit(1)
        return

    vid_id = args.video_id
    if vid_id is None:
        async with get_db_context() as db:
            res = await db.execute(select(Video).where(Video.is_active == True))
            video = res.scalar_one_or_none()
            if not video:
                print("❌ Error: No active video found in database.")
                sys.exit(1)
            vid_id = video.id

    print(f"🚀 Starting Test Upload for Video ID {vid_id} (Privacy: {args.privacy})...")
    print("⏳ Processing FFmpeg overlays & uploading to YouTube...")

    try:
        result = await test_upload_video(
            video_id=vid_id,
            privacy=args.privacy,
            send_discord=args.discord,
        )
        print("\n" + "=" * 55)
        print("  🎉 TEST UPLOAD SUCCESSFUL!")
        print("=" * 55)
        print(f"  📺 Channel:     {result.get('channel_name')}")
        print(f"  🆔 Video ID:    {result.get('youtube_video_id')}")
        print(f"  🔗 Watch URL:   {result.get('youtube_url')}")
        print(f"  🔒 Privacy:     {result.get('privacy')}")
        print(f"  ⏱️ Time Taken:  {result.get('duration_seconds')}s")
        print("=" * 55 + "\n")
    except Exception as e:
        print(f"\n❌ Test Upload Failed: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
