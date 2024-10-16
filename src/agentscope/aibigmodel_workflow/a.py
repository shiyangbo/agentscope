def validate_node_condition(node_id, conditions):
    print(f"Switch Node判断池 {conditions}")
    for condition in conditions:
        if node_id in condition and condition[node_id]:
            print(f"Switch Node判断池 条件中有且为True")
            return True
        elif node_id not in condition:
            print(f"Switch Node判断池 条件中没有")
            return True
    return False
    # if any(node_id in d and d[node_id] for d in conditions):
    #     print(f"===== 进入True")
    #     return True
    # elif any(node_id not in d for d in conditions):
    #     return True
    # else:
    #     return False


# conditions = [{'pythonnode': True}, {'pythonnode': False}]
# node_id = 'pythonnode'
# a = validate_node_condition(node_id, conditions)
# print(a)
# for condi in conditions:
#     print(condi[node_id])
#     if node_id in condi and condi[node_id]:
#         print(123123)


my_dict = {"source_id": {"target_id": [True, True]}}

my_dict2 = list()
my_dict2.append({"target_node_id": True})
my_dict2.append({"target_node_id2": False})
print(my_dict2)
value = my_dict["source_id"]["target_id"]
my_dict["source_id"]["key"] = [True]
print(my_dict)  # 输出：True

dict1 =[{'target_node_id': True}, {'target_node_id2': False}]
dict2 = {'source_id': {'target_id': [True, True], 'key': [True]}}




dicr = {}
dict = {'switchnode': {'endnode': True}}
dict['switchnode']['endnode'] = True
dict['switchnode']['python1node'] = True
print(dict)

print("1" in "123")