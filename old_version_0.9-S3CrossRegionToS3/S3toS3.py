# -*- coding: utf-8 -*-
# Python 3.6
# Composed by Huang Zhuobin
# Cross copy object between China region AWS S3 and Global region AWS S3
# install boto3 refer to https://github.com/boto/boto3

import sys
import os
import json
import boto3
from concurrent import futures
from botocore.exceptions import ClientError, EndpointConnectionError
import time
from S3toS3_config import *
import hashlib

SRCsession = boto3.session.Session(profile_name=src_aws_profile_name)
s3SRCclient = SRCsession.client('s3')
DESsession = boto3.session.Session(profile_name=des_aws_profile_name)
s3DESclient = DESsession.client('s3')

# Split file into index parts
def split(srcfile):
    partnumber = 1
    indexList = [0]
    while chunksize * partnumber < srcfile["Size"]:
        indexList.append(chunksize * partnumber)
        partnumber += 1
    if partnumber > 10000:
        print(f'\n[ERROR] PART NUMBER LIMIT 10,000. YOUR FILE HAS {partnumber}. ')
        print('PLEASE CHANGE THE chunksize IN CONFIG FILE AND TRY AGAIN')
        os._exit(0)
    return indexList

# Create multipart upload
def createUpload(srcfile):
    response = s3DESclient.create_multipart_upload(
        Bucket=desBucket,
        Key=srcfile["Key"],
        StorageClass=StorageClass
    )
    print ("[INFO] Create_multipart_upload UploadId: ",response["UploadId"])
    return response["UploadId"]

# Single Thread Upload one part


def uploadThread(uploadId, partnumber, partStartIndex, srcfileKey, total, md5list, dryrun, complete_list):
    if ifVerifyMD5 == True or (ifVerifyMD5 == False and dryrun == False):
        # 下载文件
        if dryrun == False:
            print("Downloading", str(partnumber)+"/" + str(total), "...")
        else:
            print("Downloading for verify MD5", str(partnumber)+"/" + str(total), "...")
        retryTime = 0
        while retryTime <= MaxRetry:
            try:
                response_get_object = s3SRCclient.get_object(
                    Bucket=srcBucket,
                    Key=srcfileKey,
                    Range="bytes="+str(partStartIndex)+"-"+str(partStartIndex+chunksize-1)
                    )
                getBody = response_get_object["Body"].read()
                md5list[partnumber-1] = hashlib.md5(getBody)
                break
            except Exception as e:
                retryTime += 1
                print("[WARNING] DownloadThreadFunc Exception log: ", str(e))
                print ("[WARNING] Download part fail, retry part: ",str(partnumber),"Attempts: ",str(retryTime))
                if retryTime > MaxRetry:
                    print("[ERROR] Quit for Max Download retries: ",str(retryTime))
                    os._exit(0)
                time.sleep(5*retryTime)  # 递增延迟重试
    if dryrun == False:
        # 上传文件
        print(f'               Uploading {partnumber}/{total} ...')
        retryTime = 0
        while retryTime <= MaxRetry:
            try:
                s3DESclient.upload_part(
                    Body=getBody,
                    Bucket=desBucket,
                    Key=srcfileKey,
                    PartNumber=partnumber,
                    UploadId=uploadId
                )
                break
            except Exception as e:
                retryTime += 1
                print("[WARNING] UploadThreadFunc log:", str(e))
                print ("[WARNING] Upload part fail, retry part: ",str(partnumber),"Attempts: ",str(retryTime))
                if retryTime > MaxRetry:
                    print("[ERROR] Quit for Max Upload retries: ",str(retryTime))
                    os._exit(0)
                time.sleep(5*retryTime)  # 递增延迟重试
    complete_list.append(partnumber)
    if dryrun == False:
        print(
            f'                                 Complete {partnumber}/{total} {len(complete_list)/total:.2%}')
    return

# Recursive upload parts
def uploadPart(uploadId, indexList, partnumberList, srcfile):
    partnumber = 1  # 当前循环要上传的Partnumber
    total = len(indexList)
    md5list = [hashlib.md5(b'')]*total
    complete_list = []
    # 线程池Start
    with futures.ThreadPoolExecutor(max_workers=MaxThread) as pool:
        for partStartIndex in indexList:
            # start to upload part
            if partnumber not in partnumberList:
                dryrun = False
            else:
                dryrun = True
            # upload 1 part/thread
            pool.submit(uploadThread, uploadId, partnumber,
                        partStartIndex, srcfile["Key"], total, md5list, dryrun, complete_list)
            partnumber += 1
    # 线程池End
    print(f'[INFO] All parts uploaded, size: {srcfile["Size"]}')

    # 计算所有分片列表的总etag: cal_etag
    digests = b"".join(m.digest() for m in md5list)
    md5full = hashlib.md5(digests)
    cal_etag = '"%s-%s"' % (md5full.hexdigest(), len(md5list))

    return cal_etag

# Complete multipart upload
# 通过查询回来的所有Part列表uploadedListParts来构建completeStructJSON
def completeUpload(reponse_uploadId, srcfileKey, len_indexList):
    # 查询S3的所有Part列表uploadedListParts构建completeStructJSON
    uploadedListPartsClean = []
    PartNumberMarker = 0
    IsTruncated = True
    while IsTruncated == True:
        response_uploadedList = s3DESclient.list_parts(
            Bucket=desBucket,
            Key=srcfileKey,
            UploadId=reponse_uploadId,
            MaxParts=1000,
            PartNumberMarker=PartNumberMarker
        )
        NextPartNumberMarker = response_uploadedList['NextPartNumberMarker']
        IsTruncated = response_uploadedList['IsTruncated']
        if NextPartNumberMarker > 0:
            for partObject in response_uploadedList["Parts"]:
                ETag = partObject["ETag"]
                PartNumber = partObject["PartNumber"]
                addup = {
                    "ETag": ETag,
                    "PartNumber": PartNumber
                }
                uploadedListPartsClean.append(addup)
        PartNumberMarker = NextPartNumberMarker
    if len(uploadedListPartsClean) != len_indexList:
        print("[WARNING] Uploaded parts size not match")
        os._exit(0)
    completeStructJSON = {"Parts": uploadedListPartsClean}

    # S3合并multipart upload任务
    response = s3DESclient.complete_multipart_upload(
        Bucket=desBucket,
        Key=srcfileKey,
        UploadId=reponse_uploadId,
        MultipartUpload=completeStructJSON
    )
    #print("Complete all upload and merged. UploadId: ", reponse_uploadId)
    return response

# 查询S3API 已上传的Partnumber
def checkPartnumberList(srcfile, reponse_uploadId):
    try:
        partnumberList = []
        PartNumberMarker = 0
        IsTruncated = True
        while IsTruncated == True:
            response_uploadedList = s3DESclient.list_parts(
                Bucket=desBucket,
                Key=srcfile["Key"],
                UploadId=reponse_uploadId,
                MaxParts=1000,
                PartNumberMarker=PartNumberMarker
            )
            NextPartNumberMarker = response_uploadedList['NextPartNumberMarker']
            IsTruncated = response_uploadedList['IsTruncated']
            if NextPartNumberMarker > 0:
                for partnumberObject in response_uploadedList["Parts"]:
                    partnumberList.append(partnumberObject["PartNumber"])
            PartNumberMarker = NextPartNumberMarker
        if partnumberList != []:  # 如果为0则表示没有查到已上传的Part
            print("[INFO] Got partnumber list: ", partnumberList)
    except Exception as e:
        print("[ERROR] Exception err, quit \n"+str(e))
        os._exit(0)
    return partnumberList

# 获取源文件列表，含Key和文件Size
def getSRCFileList():
    fileList = []
    # 原文件名为*则查文件列表，否则就查单个文件
    try:
        if srcfileIndex == "*":
            response_fileList = s3SRCclient.list_objects_v2(
                Bucket=srcBucket,
                Prefix=srcPrefix,
                MaxKeys=1000
            )
            for n in response_fileList["Contents"]:
                # 检查文件大小，小于单个分片大小的从列表中去掉（如果IgnoreSmallFile开关打开）
                if (n["Size"] >= chunksize) or (IgnoreSmallFile == 0):
                    if n["Key"][-1] != '/':      # Key以"/“结尾的是子目录，不处理
                        fileList.append({
                            "Key": n["Key"],
                            "Size": n["Size"]
                        })
            while response_fileList["IsTruncated"]:
                response_fileList = s3SRCclient.list_objects_v2(
                    Bucket=srcBucket,
                    Prefix=srcPrefix,
                    MaxKeys=1000,
                    ContinuationToken=response_fileList["NextContinuationToken"]
                )
                for n in response_fileList["Contents"]:
                    # 检查文件大小，小于单个分片大小的从列表中去掉（如果IgnoreSmallFile开关打开）
                    if (n["Size"] >= chunksize) or (IgnoreSmallFile == 0):
                        if n["Key"][-1] != '/':      # Key以"/“结尾的是子目录，不处理
                            fileList.append({
                                "Key": n["Key"],
                                "Size": n["Size"]
                            })
        else:
            response_fileList = s3SRCclient.head_object(
                Bucket=srcBucket,
                Key=os.path.join(srcPrefix,srcfileIndex)
            )
            fileList = [{
                "Key": os.path.join(srcPrefix,srcfileIndex),
                "Size": response_fileList["ContentLength"]
            }]
    except Exception as e:
        print('[ERROR] Can not get source bucket/prefix. Err: ',e)
        os._exit(0)
    return fileList

# 获取目标文件列表，含Key和文件Size
def getDESFileList():
    fileList = []
    response_fileList = s3DESclient.list_objects_v2(
        Bucket=desBucket,
        Prefix=srcPrefix,
        MaxKeys=1000
    )
    for n in response_fileList["Contents"]:
        if n["Key"][-1] != '/':      # Key以"/“结尾的是子目录，不处理
            fileList.append({
                "Key": n["Key"],
                "Size": n["Size"]
            })
    while response_fileList["IsTruncated"]:
        response_fileList = s3DESclient.list_objects_v2(
            Bucket=desBucket,
            Prefix=srcPrefix,
            MaxKeys=1000,
            ContinuationToken=response_fileList["NextContinuationToken"]
        )
        for n in response_fileList["Contents"]:
            if n["Key"][-1] != '/':      # Key以"/“结尾的是子目录，不处理
                fileList.append({
                    "Key": n["Key"],
                    "Size": n["Size"]
                })
    return fileList

# 查找Key是否存在，并且Size一致
def checkFileExist(srcfile, desFilelist, UploadIdList):
    # 检查源文件是否在目标文件夹中
    for f in desFilelist:
        if f["Key"] == srcfile["Key"] and \
            (srcfile["Size"] == f["Size"]):  
            return 'NEXT'  # 文件完全相同
    # 找不到文件，或文件不一致，要重新传的
    # 查Key是否有未完成的UploadID
    keyIDList=[]
    for u in UploadIdList:
        if u["Key"] == srcfile["Key"]:
            keyIDList.append(u)
    # 如果找不到上传过的Upload，则从头开始传
    if keyIDList == []:
        return 'UPLOAD'
    # 对同一个Key（文件）的不同Upload找出时间最晚的值
    UploadID_latest = keyIDList[0]
    for u in keyIDList:
        if u["Initiated"] > UploadID_latest["Initiated"]:
            UploadID_latest = u
    return UploadID_latest["UploadId"]

# 获取Bucket/Prefix中所有未完成的Multipart Upload
def getUploadIdList():
    NextKeyMarker=''
    IsTruncated = True
    UploadIdList=[]
    while IsTruncated:
        response = s3DESclient.list_multipart_uploads(
            Bucket=desBucket,
            Prefix=srcPrefix,
            MaxUploads=1000,
            KeyMarker=NextKeyMarker
        )
        IsTruncated = response["IsTruncated"]
        NextKeyMarker = response["NextKeyMarker"]
        if NextKeyMarker != '':
            for n in response["Uploads"]:
                UploadIdList.append({
                    "Key": n["Key"],
                    "Initiated": n["Initiated"],
                    "UploadId": n["UploadId"]
                })
                print(f'[INFO] Unfinished upload: Key: {n["Key"]}, Time: {n["Initiated"]}')
    return UploadIdList


class NextFile(Exception):
    pass

# Main
if __name__ == '__main__':
    # 检查目标S3能否写入
    try:
        s3DESclient.put_object(
            Bucket=desBucket,
            Key=os.path.join(srcPrefix, 'access_test'),
            Body='access_test_content'
        )
    except Exception as e:
        print('[ERROR] Not authorized to write to destination bucket/prefix. Err: ', e)
        os._exit(0)

    # 获取源文件列表和目标文件夹现存文件列表
    fileList = getSRCFileList()
    desFilelist = getDESFileList()
    # 获取Bucket/Prefix中所有未完成的Multipart Upload
    UploadIdList=getUploadIdList()

    # 是否清理所有未完成的Multipart Upload, 用于强制重传
    if UploadIdList != []:
        print(f'[WARNING] There are {len(UploadIdList)} unfinished upload, do you want to clean them and restart?')
        print('NOTICE: IF CLEAN, YOU CANNOT RESUME ANY UNFINISHED UPLOAD')
        keyboard_input = input("CLEAN? Please confirm: (n/CLEAN)")
        if keyboard_input == 'CLEAN':
            # 清理所有未完成的Upload
            for n in UploadIdList:
                response = s3DESclient.abort_multipart_upload(
                    Bucket=desBucket,
                    Key=n["Key"],
                    UploadId=n["UploadId"]
                )
            UploadIdList=[]
            print('[INFO] CLEAN FINISHED')
        else:
            print('[INFO] Do not clean, try to resume unfinished upload')

    # 对文件列表fileList中的逐个文件进行操作
    for srcfile in fileList:
        print('')

        try:
            # 循环重试3次（如果MD5计算的ETag不一致）
            for md5_retry in range(0, 3):

                # 检查文件是否已存在，存在不继续、不存在且没UploadID要新建、不存在但有UploadID得到返回的UploadID
                response_check_upload=checkFileExist(srcfile, desFilelist, UploadIdList)
                if response_check_upload == 'UPLOAD':
                    reponse_uploadId = createUpload(srcfile)
                    print(f'[INFO] For new upload: {srcfile["Key"]}')
                    partnumberList=[]
                elif response_check_upload == 'NEXT':
                    print(
                        f'[INFO] Duplicated. {srcfile["Key"]} already exist, and same size. Handle next file.')
                    raise NextFile()
                else:
                    reponse_uploadId=response_check_upload
                    # 获取已上传partnumberList
                    partnumberList = checkPartnumberList(srcfile, reponse_uploadId)

                # 获取索引列表
                response_indexList = split(srcfile)

                # 执行分片upload
                upload_etag_full = uploadPart(
                    reponse_uploadId, response_indexList, partnumberList, srcfile)

                # 合并S3上的文件
                response_complete = completeUpload(
                    reponse_uploadId, srcfile["Key"], len(response_indexList))
                print(
                    f'[INFO] FINISH: {srcfile} UPLOADED TO {response_complete["Location"]}')

                # 检查文件MD5
                if ifVerifyMD5 == True:
                    if response_complete["ETag"] == upload_etag_full:
                        print('[INFO] MD5 ETag Matched: ',
                            response_complete["ETag"], '\n')
                        break
                    else:  # ETag 不匹配，删除S3的文件，重试
                        print('[WARNING] MD5 ETag NOT MATCHED ( Destination / Origin ): ',
                            response_complete["ETag"], '/', upload_etag_full)
                        s3DESclient.delete_object(
                            Bucket=desBucket,
                            Key=srcfile["Key"]
                        )
                        UploadIdList = []
                        print('[WARNING] Deleted and retry upload...')
                    if md5_retry == 2:
                        print('[ERROR] MD5 ETag NOT MATCHED Exceed Max Retries!\n')
                else:
                    break
        except NextFile:
            pass
    print(
        f'\n[INFO] COPY MISSION ACCOMPLISHED, FROM (BUCKET/PREFIX): {srcBucket}/{srcPrefix} TO {desBucket}/{srcPrefix}')

    # 再次获取源文件列表和目标文件夹现存文件列表进行比较，输出比较结果
    print('[INFO] Comparing destination and source ...')
    fileList = getSRCFileList()
    desFilelist = getDESFileList()
    deltaList = []
    for source_file in fileList:
        match = False
        for destination_file in desFilelist:
            if source_file == destination_file:
                match = True # source 在 destination找到，并且Size一致
                break
        if not match:
            deltaList.append(source_file)
    if deltaList==[]:
        print(f'[INFO] All source files are in destination Bucket/Prefix')
    else:
        print(f'[WARNING] There are {len(deltaList)} files not in destination or not the same size, list:')
        for delta_file in deltaList:
            print(delta_file)
